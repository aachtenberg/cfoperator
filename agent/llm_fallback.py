"""
LLM Fallback Manager with Exponential Backoff

Provides resilient LLM access with:
- Ordered fallback chain through local Ollama instances
- Optional paid LLM escalation as last resort
- Exponential backoff to avoid hammering failed endpoints
- Persistent cooldown state in database

Inspired by OpenClaw's model-fallback architecture, simplified for homelab use.
"""
import json
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Tuple, Callable
import requests
from sqlalchemy import text


def _log(level: str, msg: str, **fields: Any) -> None:
    """Structured logging matching agent pattern."""
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "component": "llm_fallback",
        "msg": msg,
        **fields
    }
    print(json.dumps(payload, ensure_ascii=False))


# Provider SDKs report the HTTP status differently: the OpenAI/Anthropic/Groq
# clients expose it on the exception, requests puts it on the response, and some
# wrappers only ever put it in the message text.
_STATUS_IN_MESSAGE = re.compile(
    r"\b(?:error code|status(?:_code)?|http)\D{0,3}([1-5]\d{2})\b", re.IGNORECASE)


def _status_code_of(error: Exception) -> Optional[int]:
    """Best-effort HTTP status for an SDK exception, or None."""
    for candidate in (getattr(error, "status_code", None),
                      getattr(getattr(error, "response", None), "status_code", None)):
        if isinstance(candidate, int):
            return candidate
    match = _STATUS_IN_MESSAGE.search(str(error))
    return int(match.group(1)) if match else None


class LLMCircuitOpen(RuntimeError):
    """Raised instead of walking the chain while the circuit is open (CFOP-321)."""

    def __init__(self, until: float, reason: str):
        self.until = until
        stamp = datetime.fromtimestamp(until, timezone.utc).isoformat(timespec="seconds")
        super().__init__(f"LLM circuit open until {stamp}: {reason}")


CIRCUIT_CLOSED = "closed"
CIRCUIT_OPEN = "open"
CIRCUIT_HALF_OPEN = "half_open"


class ChainCircuitBreaker:
    """Fail fast after the whole provider chain keeps failing (CFOP-321).

    Per-provider cooldowns say nothing about the chain as a whole: when the
    network is gone or every key is exhausted, each investigation still walks
    every rung with its own timeout, holds the investigation lock while it
    does, and hits every vendor again. After ``failure_threshold`` exhausted
    walks within ``window_seconds`` the breaker opens and calls fail at once
    for ``cooldown_seconds``. The next call after that is a single probe:
    success closes the breaker, failure re-opens it. Callers arriving while
    the probe is in flight fail fast too.

    The window defaults to 30 minutes, not the few minutes a breaker usually
    gets. A walk that ends in timeouts takes up to rungs x 180s, so a short
    window could only ever trip on fast failures and never on the slow
    pile-up this exists for.

    State is in memory. One agent per database is enforced (CFOP-322), and a
    restart closing the breaker costs at most one more walk.
    """

    DEFAULT_FAILURE_THRESHOLD = 3
    DEFAULT_WINDOW_SECONDS = 1800
    DEFAULT_COOLDOWN_SECONDS = 300

    def __init__(self, failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
                 window_seconds: float = DEFAULT_WINDOW_SECONDS,
                 cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
                 clock: Callable[[], float] = time.time):
        self.failure_threshold = failure_threshold
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: deque = deque()
        self._state = CIRCUIT_CLOSED
        self._open_until = 0.0
        self._probing = False

    @classmethod
    def from_config(cls, cfg: Optional[Dict[str, Any]], **kwargs) -> "ChainCircuitBreaker":
        """Build from ``llm.circuit_breaker``; absent keys take the defaults.

        A value that is not a non-negative number falls back to its default
        with a warning rather than failing the boot over a typo.
        """
        cfg = cfg if isinstance(cfg, dict) else {}
        values = {}
        for key, default in (("failure_threshold", cls.DEFAULT_FAILURE_THRESHOLD),
                             ("window_seconds", cls.DEFAULT_WINDOW_SECONDS),
                             ("cooldown_seconds", cls.DEFAULT_COOLDOWN_SECONDS)):
            raw = cfg.get(key, default)
            try:
                value = int(raw) if key == "failure_threshold" else float(raw)
                if isinstance(raw, bool) or value < 0:
                    raise ValueError(raw)
            except (TypeError, ValueError):
                _log("warn", "Invalid llm.circuit_breaker value, using default",
                     key=key, value=str(raw), default=default)
                value = default
            values[key] = value
        return cls(**values, **kwargs)

    @property
    def enabled(self) -> bool:
        return self.failure_threshold > 0

    @property
    def state(self) -> str:
        return self._state

    def before_call(self) -> None:
        """Raise LLMCircuitOpen unless this caller may walk the chain."""
        if not self.enabled:
            return
        with self._lock:
            now = self._clock()
            if self._state == CIRCUIT_OPEN:
                if now < self._open_until:
                    raise LLMCircuitOpen(self._open_until, "the provider chain kept failing")
                self._state = CIRCUIT_HALF_OPEN
                self._probing = False
            if self._state == CIRCUIT_HALF_OPEN:
                if self._probing:
                    raise LLMCircuitOpen(now + self.cooldown_seconds,
                                         "a probe call is already testing the chain")
                self._probing = True

    def record_success(self) -> None:
        """The chain answered: close, and forget earlier failures."""
        with self._lock:
            self._state = CIRCUIT_CLOSED
            self._failures.clear()
            self._probing = False

    def record_failure(self) -> bool:
        """Every rung failed. Returns True when this failure opened the circuit."""
        if not self.enabled:
            return False
        with self._lock:
            now = self._clock()
            if self._state == CIRCUIT_HALF_OPEN:
                self._open(now)
                return True
            self._failures.append(now)
            while self._failures and self._failures[0] <= now - self.window_seconds:
                self._failures.popleft()
            if len(self._failures) >= self.failure_threshold:
                self._open(now)
                return True
            return False

    def release_probe(self) -> None:
        """A walk ended without a verdict on the chain (the caller raised
        mid-walk). Let the next caller probe instead of failing fast forever."""
        with self._lock:
            if self._state == CIRCUIT_HALF_OPEN:
                self._probing = False

    def _open(self, now: float) -> None:
        self._state = CIRCUIT_OPEN
        self._open_until = now + self.cooldown_seconds
        self._probing = False
        self._failures.clear()
        _log("warn", "LLM circuit opened",
             until=datetime.fromtimestamp(self._open_until, timezone.utc).isoformat(),
             cooldown_seconds=self.cooldown_seconds)


class LLMFallbackManager:
    """
    Manages LLM provider selection with fallback and exponential backoff.

    Flow:
    1. Try each provider in llm_fallback_chain (local Ollama instances)
    2. If all local providers in cooldown and allow_paid_escalation=True,
       try the single paid_llm_escalation provider
    3. If all exhausted, return None (caller should buffer/fail gracefully)
    """

    # Error classification patterns
    TIMEOUT_PATTERNS = ["timeout", "timed out", "etimedout", "read timed out"]
    RATE_LIMIT_PATTERNS = ["rate limit", "rate_limit", "quota", "too many requests", "resource exhausted"]
    AUTH_PATTERNS = ["unauthorized", "invalid api key", "authentication", "forbidden"]
    # Malformed/rejected requests (HTTP 4xx other than 401/403/429). These are a
    # code/payload bug, not provider ill-health, so they must not be treated as
    # a transient "connection" error and earn exponential cooldown on an
    # otherwise-healthy provider.
    BAD_REQUEST_PATTERNS = ["invalid_request", "invalid request", "bad request", "unprocessable"]

    # Cooldown configuration
    MAX_COOLDOWN_MINUTES = 60
    BACKOFF_BASE = 5  # Multiplier for exponential backoff
    ERROR_COUNT_RESET_HOURS = 1  # Reset error count after this many hours of no failures

    def __init__(self, db_session_factory: Callable, settings_getter: Callable,
                 circuit: Optional[ChainCircuitBreaker] = None):
        """
        Initialize fallback manager.

        Args:
            db_session_factory: Callable that returns a context manager for DB sessions
            settings_getter: Callable that returns current agent settings dict
            circuit: Whole-chain breaker consulted by the agent's chain walk
                (CFOP-321); defaults to one with the default thresholds
        """
        self.db_session_factory = db_session_factory
        self.get_settings = settings_getter
        self.circuit = circuit or ChainCircuitBreaker()
        self._ensure_table()

    def _ensure_table(self) -> None:
        """Create llm_provider_state table if it doesn't exist."""
        try:
            with self.db_session_factory() as session:
                session.execute(text("""
                    CREATE TABLE IF NOT EXISTS llm_provider_state (
                        provider_key VARCHAR(255) PRIMARY KEY,
                        cooldown_until TIMESTAMP,
                        error_count INTEGER DEFAULT 0,
                        last_error_at TIMESTAMP,
                        last_error_reason TEXT,
                        last_success_at TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """))
                session.commit()
        except Exception as e:
            _log("warn", "Could not ensure llm_provider_state table", error=str(e))
            return

        self._widen_last_error_reason()

    def _widen_last_error_reason(self) -> None:
        """Migrate a pre-existing VARCHAR(50) last_error_reason to TEXT.

        Separate transaction from table creation on purpose: the ALTER needs
        table ownership, and rolling it in with CREATE TABLE meant an
        ownership failure discarded the creation too, leaving a fresh database
        with no table at all. Conditional so a converged column doesn't take an
        ACCESS EXCLUSIVE lock on every start.
        """
        try:
            with self.db_session_factory() as session:
                stale = session.execute(text("""
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'llm_provider_state'
                      AND column_name = 'last_error_reason'
                      AND data_type <> 'text'
                """)).fetchone()
                if not stale:
                    return
                session.execute(text("""
                    ALTER TABLE llm_provider_state
                    ALTER COLUMN last_error_reason TYPE TEXT
                """))
                session.commit()
            _log("info", "Widened llm_provider_state.last_error_reason to TEXT")
        except Exception as e:
            _log("warn", "Could not widen last_error_reason to TEXT", error=str(e))

    def classify_error(self, error: Exception, status_code: int = None) -> str:
        """
        Classify error type for appropriate backoff strategy.

        Args:
            error: The exception that occurred
            status_code: HTTP status code; read off the exception when omitted,
                since neither call site has it to hand and the message patterns
                alone miss SDKs that report only a bare code

        Returns:
            Error type: 'timeout', 'connection', 'rate_limit', 'auth', or
            'bad_request'
        """
        # Check by exception type first
        if isinstance(error, requests.Timeout):
            return "timeout"
        if isinstance(error, requests.ConnectionError):
            return "connection"

        # Check by HTTP status code
        status_code = status_code or _status_code_of(error)
        if status_code:
            if status_code == 429:
                return "rate_limit"
            if status_code in (401, 403):
                return "auth"
            # Any other 4xx is a client-side/payload error, not provider health.
            if 400 <= status_code < 500:
                return "bad_request"

        # Check by error message patterns
        msg = str(error).lower()

        for pattern in self.TIMEOUT_PATTERNS:
            if pattern in msg:
                return "timeout"

        for pattern in self.RATE_LIMIT_PATTERNS:
            if pattern in msg:
                return "rate_limit"

        for pattern in self.AUTH_PATTERNS:
            if pattern in msg:
                return "auth"

        for pattern in self.BAD_REQUEST_PATTERNS:
            if pattern in msg:
                return "bad_request"

        # Default to connection error
        return "connection"

    def calculate_cooldown(self, error_count: int, error_type: str) -> timedelta:
        """
        Calculate cooldown duration using exponential backoff.

        Formula: min(60, 1 * 5^(error_count-1)) minutes
        - 1st error: 1 min
        - 2nd error: 5 min
        - 3rd error: 25 min
        - 4th+ error: 60 min (capped)

        Auth and bad_request errors get a shorter fixed cooldown since they're
        config/code issues rather than provider ill-health.

        Args:
            error_count: Number of consecutive errors
            error_type: Type of error ('auth'/'bad_request' get special handling)

        Returns:
            Cooldown duration as timedelta
        """
        if error_type in ("auth", "bad_request"):
            # Config/code issues, not provider health — a short fixed cooldown so
            # a bad key or a malformed request doesn't sideline an otherwise
            # healthy provider under exponential backoff.
            return timedelta(minutes=5)

        # Exponential backoff for other errors
        minutes = min(
            self.MAX_COOLDOWN_MINUTES,
            1 * (self.BACKOFF_BASE ** (error_count - 1))
        )
        return timedelta(minutes=minutes)

    def _get_provider_state(self, provider_key: str) -> Optional[Dict[str, Any]]:
        """Get current state for a provider from database."""
        try:
            with self.db_session_factory() as session:
                result = session.execute(text("""
                    SELECT provider_key, cooldown_until, error_count,
                           last_error_at, last_error_reason, last_success_at
                    FROM llm_provider_state
                    WHERE provider_key = :key
                """), {"key": provider_key}).fetchone()

                if result:
                    return {
                        "provider_key": result[0],
                        "cooldown_until": result[1],
                        "error_count": result[2] or 0,
                        "last_error_at": result[3],
                        "last_error_reason": result[4],
                        "last_success_at": result[5],
                    }
        except Exception as e:
            _log("debug", "Failed to get provider state", provider=provider_key, error=str(e))
        return None

    def record_failure(self, provider_key: str, reason: str) -> None:
        """
        Record a failure for a provider and update cooldown.

        Args:
            provider_key: Provider identifier (e.g., "ollama/localhost/qwen3:14b")
            reason: Error type from classify_error()
        """
        # calculate_cooldown keys off the short bucket, so a caller passing raw
        # exception text would both skew the backoff and (pre-TEXT) break the
        # INSERT. Clamp here so the contract can't be violated from outside.
        reason = (str(reason) if reason else "unknown")[:64]
        try:
            now = datetime.now(timezone.utc)
            state = self._get_provider_state(provider_key)

            if state:
                # Check if we should reset error count (no failures in reset window)
                error_count = state["error_count"]
                last_error = state["last_error_at"]
                if last_error:
                    # Make last_error timezone-aware if it isn't
                    if last_error.tzinfo is None:
                        last_error = last_error.replace(tzinfo=timezone.utc)
                    if now - last_error > timedelta(hours=self.ERROR_COUNT_RESET_HOURS):
                        error_count = 0

                error_count += 1
            else:
                error_count = 1

            cooldown = self.calculate_cooldown(error_count, reason)
            cooldown_until = now + cooldown

            with self.db_session_factory() as session:
                session.execute(text("""
                    INSERT INTO llm_provider_state
                        (provider_key, cooldown_until, error_count, last_error_at, last_error_reason, updated_at)
                    VALUES (:key, :cooldown, :count, :now, :reason, :now)
                    ON CONFLICT (provider_key) DO UPDATE SET
                        cooldown_until = EXCLUDED.cooldown_until,
                        error_count = EXCLUDED.error_count,
                        last_error_at = EXCLUDED.last_error_at,
                        last_error_reason = EXCLUDED.last_error_reason,
                        updated_at = EXCLUDED.updated_at
                """), {
                    "key": provider_key,
                    "cooldown": cooldown_until,
                    "count": error_count,
                    "now": now,
                    "reason": reason
                })
                session.commit()

            _log("info", "Provider marked in cooldown",
                 provider=provider_key,
                 reason=reason,
                 error_count=error_count,
                 cooldown_minutes=cooldown.total_seconds() / 60)

        except Exception as e:
            _log("error", "Failed to record failure", provider=provider_key, error=str(e))

    def record_success(self, provider_key: str) -> None:
        """
        Record a successful call to a provider.

        Updates last_success_at but doesn't immediately reset error_count.
        Error count resets naturally after ERROR_COUNT_RESET_HOURS of no failures.

        Args:
            provider_key: Provider identifier
        """
        try:
            now = datetime.now(timezone.utc)
            with self.db_session_factory() as session:
                session.execute(text("""
                    INSERT INTO llm_provider_state
                        (provider_key, last_success_at, updated_at, error_count)
                    VALUES (:key, :now, :now, 0)
                    ON CONFLICT (provider_key) DO UPDATE SET
                        last_success_at = EXCLUDED.last_success_at,
                        updated_at = EXCLUDED.updated_at,
                        cooldown_until = NULL
                """), {"key": provider_key, "now": now})
                session.commit()
        except Exception as e:
            _log("debug", "Failed to record success", provider=provider_key, error=str(e))

    def is_available(self, provider_key: str) -> bool:
        """
        Check if a provider is available (not in cooldown).

        Args:
            provider_key: Provider identifier

        Returns:
            True if provider can be used, False if in cooldown
        """
        state = self._get_provider_state(provider_key)
        if not state:
            return True  # Never seen, assume available

        cooldown_until = state.get("cooldown_until")
        if not cooldown_until:
            return True

        # Make cooldown_until timezone-aware if it isn't
        if cooldown_until.tzinfo is None:
            cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        return now >= cooldown_until

    def get_fallback_chain(self) -> List[str]:
        """Get the configured fallback chain from settings."""
        settings = self.get_settings()
        chain = settings.get("llm_fallback_chain", [])

        # Handle string format (newline-separated) or list
        if isinstance(chain, str):
            chain = [p.strip() for p in chain.split("\n") if p.strip()]

        return chain

    def parse_provider_key(self, provider_key: str) -> Optional[Tuple[str, Optional[str], str]]:
        """
        Parse provider key into (type, url, model).

        Formats:
        - "ollama/localhost/qwen3:14b" -> ("ollama", "http://localhost:11434", "qwen3:14b")
        - "groq/llama-3.3-70b-versatile" -> ("groq", None, "llama-3.3-70b-versatile")
        - "gemini/gemini-2.0-flash" -> ("gemini", None, "gemini-2.0-flash")

        Args:
            provider_key: Provider identifier string

        Returns:
            Tuple of (provider_type, url_or_none, model) or None if invalid
        """
        parts = provider_key.split("/")
        if len(parts) < 2:
            return None

        provider_type = parts[0].lower()

        if provider_type == "ollama":
            if len(parts) < 3:
                return None
            # Ollama format: ollama/host/model
            host = parts[1]
            model = "/".join(parts[2:])  # Handle models with slashes
            # Add port if not specified
            if ":" not in host:
                host = f"{host}:11434"
            return ("ollama", f"http://{host}", model)
        else:
            # Cloud providers: provider/model
            model = "/".join(parts[1:])
            return (provider_type, None, model)

    def get_next_provider(self) -> Optional[Tuple[str, Optional[str], str]]:
        """
        Get the next available provider.

        Respects enable_local_ollama and allow_paid_escalation settings.
        Tries local Ollama instances first (in order), then paid escalation
        if enabled and all local providers are in cooldown.

        Returns:
            Tuple of (provider_type, url_or_none, model) or None if all exhausted
        """
        settings = self.get_settings()
        chain = self.get_fallback_chain()

        # Check if local Ollama is enabled
        enable_local = settings.get("enable_local_ollama", "true")
        local_enabled = enable_local != "false"  # Default to true

        # Try each provider in the local chain (only if local is enabled)
        if local_enabled:
            for provider_key in chain:
                # Skip paid providers in the chain - they're handled below
                parsed = self.parse_provider_key(provider_key)
                if parsed and parsed[0] == "ollama":
                    if self.is_available(provider_key):
                        _log("debug", "Selected provider", provider=provider_key)
                        return parsed

        # Check paid escalation (either as fallback or as primary if local disabled)
        allow_paid = settings.get("allow_paid_escalation", "false")
        if allow_paid == "true" or allow_paid is True:
            paid_key = settings.get("paid_llm_escalation")
            if paid_key and self.is_available(paid_key):
                parsed = self.parse_provider_key(paid_key)
                if parsed:
                    if not local_enabled:
                        _log("info", "Using paid provider (local disabled)", provider=paid_key)
                    else:
                        _log("info", "Escalating to paid provider", provider=paid_key)
                    return parsed

        _log("warn", "All LLM providers exhausted or in cooldown")
        return None

    def get_provider_status(self) -> List[Dict[str, Any]]:
        """
        Get status of all configured providers.

        Returns:
            List of provider status dicts for UI display
        """
        settings = self.get_settings()
        chain = self.get_fallback_chain()
        paid_key = settings.get("paid_llm_escalation")
        allow_paid = settings.get("allow_paid_escalation", False)

        statuses = []
        now = datetime.now(timezone.utc)

        # Add local providers
        for i, provider_key in enumerate(chain):
            state = self._get_provider_state(provider_key) or {}
            cooldown_until = state.get("cooldown_until")
            if cooldown_until and cooldown_until.tzinfo is None:
                cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)

            in_cooldown = cooldown_until and now < cooldown_until
            cooldown_remaining = None
            if in_cooldown:
                cooldown_remaining = int((cooldown_until - now).total_seconds())

            statuses.append({
                "provider_key": provider_key,
                "type": "local",
                "priority": i + 1,
                "available": not in_cooldown,
                "error_count": state.get("error_count", 0),
                "last_error_reason": state.get("last_error_reason"),
                "cooldown_remaining_seconds": cooldown_remaining,
            })

        # Add paid provider if configured
        if paid_key:
            state = self._get_provider_state(paid_key) or {}
            cooldown_until = state.get("cooldown_until")
            if cooldown_until and cooldown_until.tzinfo is None:
                cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)

            in_cooldown = cooldown_until and now < cooldown_until
            cooldown_remaining = None
            if in_cooldown:
                cooldown_remaining = int((cooldown_until - now).total_seconds())

            statuses.append({
                "provider_key": paid_key,
                "type": "paid",
                "priority": len(chain) + 1,
                "enabled": allow_paid,
                "available": allow_paid and not in_cooldown,
                "error_count": state.get("error_count", 0),
                "last_error_reason": state.get("last_error_reason"),
                "cooldown_remaining_seconds": cooldown_remaining,
            })

        return statuses

    def clear_cooldowns(self) -> int:
        """
        Clear all cooldowns (admin action).

        Returns:
            Number of providers cleared
        """
        try:
            with self.db_session_factory() as session:
                result = session.execute(text("""
                    UPDATE llm_provider_state
                    SET cooldown_until = NULL, error_count = 0
                    WHERE cooldown_until IS NOT NULL
                """))
                session.commit()
                count = result.rowcount
                _log("info", "Cleared all LLM provider cooldowns", count=count)
                return count
        except Exception as e:
            _log("error", "Failed to clear cooldowns", error=str(e))
            return 0
