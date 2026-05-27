"""HTTP-backed action handlers that delegate to an external service.

This is the event_runtime side of the alert pipeline unification. Today the
default ``investigate`` action is a stub that just records that an alert was
seen (``InvestigateActionHandler`` in defaults.py). The handler here replaces
that stub when the agent is reachable: it POSTs the alert to the agent's
``/v1/investigate`` endpoint, returns a quiet (non-notifying) ``ActionResult``
acknowledging the queued investigation, and lets the agent post the real
outcome back to ``POST /v1/investigations/{alert_id}/complete``. That
completion path fires the actual Slack notification through the normal
``_notify_action_completed`` flow.

The handler is registered from ``bootstrap`` only when ``CFOP_AGENT_URL`` is
set, so portable deployments without the agent keep the stub.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import urllib.error
import urllib.request
from typing import Optional, Tuple

from .models import ActionRequest, ActionResult, Alert, ContextEnvelope, Decision
from .plugins import ActionHandler, DecisionEngine

logger = logging.getLogger(__name__)


# ---- completion endpoint helpers -----------------------------------------
#
# Exposed here (next to the dispatch handler) so server.py and the FastAPI
# adapter share the same validation + auth path. Without this, each
# transport would re-implement the checks slightly differently.


COMPLETION_AUTH_HEADER = "X-CFOP-Token"
COMPLETION_SECRET_ENV = "CFOP_COMPLETION_SHARED_SECRET"  # noqa: S105 - env var name, not a secret


def _expected_completion_secret() -> Optional[str]:
    value = os.getenv(COMPLETION_SECRET_ENV, "").strip()
    return value or None


def verify_completion_auth(token_header: Optional[str]) -> Optional[str]:
    """Validate the auth header on a completion request.

    Returns ``None`` when the request is authorized; otherwise an error
    string suitable for a 401 response body. When the shared secret env var
    is unset, requests are accepted — portable deployments without an agent
    must remain runnable. ``log_completion_endpoint_status()`` is called once
    at startup so operators see the gap.
    """
    expected = _expected_completion_secret()
    if expected is None:
        return None
    if not token_header:
        return f"Missing {COMPLETION_AUTH_HEADER} header"
    if not secrets.compare_digest(token_header, expected):
        return f"Invalid {COMPLETION_AUTH_HEADER} header"
    return None


def log_completion_endpoint_status() -> None:
    """Emit a startup status line about completion endpoint auth.

    Called from ``build_portable_runtime`` rather than at module import time,
    so the message reaches the configured logging handlers. (Module-level
    logging fires before ``__main__.py`` runs ``logging.basicConfig`` and
    gets swallowed by the default root logger.)
    """
    if _expected_completion_secret() is None:
        logger.warning(
            "%s is not set; POST /v1/investigations/{alert_id}/complete will "
            "accept unauthenticated posts. Set this env var in production so "
            "completions can't be spoofed by other cluster pods.",
            COMPLETION_SECRET_ENV,
        )
    else:
        logger.info(
            "%s is set; POST /v1/investigations/{alert_id}/complete enforces "
            "X-CFOP-Token via constant-time comparison.",
            COMPLETION_SECRET_ENV,
        )


def parse_completion_payload(payload: object, alert_id: str) -> Tuple[Alert, ActionResult]:
    """Validate the wire shape of a completion POST and rebuild domain objects.

    Raises ``ValueError`` with a caller-safe message on any malformed input
    so HTTP transports can map it to a 400 instead of letting an
    ``AttributeError`` bubble up as a 500.
    """
    if not isinstance(payload, dict):
        raise ValueError("Body must be a JSON object")
    if "alert" not in payload or "result" not in payload:
        raise ValueError("Body must contain 'alert' and 'result' fields")
    raw_alert = payload["alert"]
    raw_result = payload["result"]
    if not isinstance(raw_alert, dict):
        raise ValueError("Field 'alert' must be a JSON object")
    if not isinstance(raw_result, dict):
        raise ValueError("Field 'result' must be a JSON object")
    alert = Alert.from_dict(raw_alert)
    if alert.alert_id != alert_id:
        raise ValueError("alert_id in path does not match alert.alert_id in body")
    action_result = ActionResult.from_dict(raw_result)
    return alert, action_result


class HTTPInvestigateActionHandler(ActionHandler):
    """Delegate ``investigate`` actions to an external agent over HTTP.

    The agent is expected to:
      1. Accept ``POST {agent_url}/v1/investigate`` with the alert body
         (matches event_runtime.models.Alert.to_dict()).
      2. Return 2xx for accepted, 5xx (or refuse the connection) when it
         can't take the work — the worker retries the latter.
      3. POST the completed ActionResult back to
         ``{event_runtime_url}/v1/investigations/{alert_id}/complete``
         when the LLM run finishes.

    The handler itself returns a quiet ActionResult so Slack only fires once,
    on completion, not twice (queued + done).
    """

    name = "http-investigate-action"
    action_name = "investigate"

    def __init__(self, agent_url: str, *, timeout: float = 5.0):
        if not agent_url:
            raise ValueError("agent_url is required")
        self._agent_url = agent_url.rstrip("/")
        self._timeout = float(timeout)

    @property
    def agent_url(self) -> str:
        return self._agent_url

    def execute(self, request: ActionRequest) -> ActionResult:
        endpoint = f"{self._agent_url}/v1/investigate"
        body = json.dumps(request.alert.to_dict(), default=str).encode("utf-8")
        try:
            self._post(endpoint, body)
        except urllib.error.HTTPError as exc:
            # 4xx is permanent (bad payload); surface as failure-without-retry.
            # 5xx is transient; raising lets the worker retry per its policy.
            if 500 <= exc.code < 600:
                logger.warning("Agent returned %s for investigate dispatch: %s", exc.code, endpoint)
                raise
            return ActionResult(
                action=self.action_name,
                success=False,
                message=f"Agent rejected investigation ({exc.code})",
                details={
                    "agent_url": self._agent_url,
                    "alert_id": request.alert.alert_id,
                    "http_status": exc.code,
                },
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.warning("Agent unreachable for investigate dispatch: %s (%s)", endpoint, type(exc).__name__)
            raise

        return ActionResult(
            action=self.action_name,
            success=True,
            message=f"Investigation dispatched to agent for: {request.alert.summary}",
            details={
                "agent_url": self._agent_url,
                "alert_id": request.alert.alert_id,
            },
            quiet=True,  # The completion post-back will fire the real Slack notification.
        )

    def _post(self, endpoint: str, body: bytes) -> None:
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            # Drain the response so the connection can be reused; status is
            # the only thing we care about beyond exception flow above.
            resp.read()


def build_http_investigate_handler(agent_url: Optional[str]) -> Optional[HTTPInvestigateActionHandler]:
    """Construct the HTTP investigate handler when an agent URL is configured.

    Returns None when ``agent_url`` is missing or empty, so callers can fall
    back to the default stub.
    """
    if not agent_url:
        return None
    return HTTPInvestigateActionHandler(agent_url=agent_url)


# ---- LLM-driven triage decision engine -----------------------------------


_TRIAGE_VALID_ACTIONS = frozenset({"log_only", "notify", "investigate", "escalate"})


class HTTPTriageDecisionEngine(DecisionEngine):
    """Decision engine that delegates triage classification to the agent.

    Replaces ``OpenReasoningDecisionEngine`` when ``CFOP_AGENT_URL`` is set.
    POSTs each alert to the agent's ``/v1/triage`` endpoint, which runs a
    short LLM call (~1-2s on a warm path, falls back through providers on
    Ollama cold-start) and returns one of: log_only, notify, investigate,
    escalate.

    Designed to never lose an alert: on any failure (agent unreachable,
    non-200, JSON parse error, invalid action label) it falls back to the
    safe default of ``investigate``. The whole point of triage is to skip
    work, so a triage outage just temporarily reverts to "investigate
    everything" rather than dropping alerts.

    Caches decisions by alert fingerprint for ``cache_ttl_seconds`` (default
    300s / 5 min) so repeat fires within the window skip the LLM round-trip.
    """

    name = "http-triage"

    def __init__(self, agent_url: str, *, timeout: float = 5.0, cache_ttl_seconds: int = 300):
        if not agent_url:
            raise ValueError("agent_url is required")
        self._agent_url = agent_url.rstrip("/")
        self._timeout = float(timeout)
        self._cache_ttl = int(cache_ttl_seconds)
        # fingerprint -> (action, reason, confidence, expires_at_unix)
        self._cache: dict[str, tuple[str, str, float, float]] = {}

    @property
    def agent_url(self) -> str:
        return self._agent_url

    def decide(self, envelope: ContextEnvelope) -> Decision:
        alert = envelope.alert
        fingerprint = alert.effective_fingerprint()

        # Cache lookup. Repeat alerts with the same labels within 5m skip
        # the LLM call entirely — important because event_runtime polls
        # Alertmanager on a 30s interval and the same alert can appear in
        # many polls before it resolves.
        cached = self._cache.get(fingerprint)
        if cached is not None:
            action, reason, confidence, expires_at = cached
            if expires_at > _now_unix():
                return Decision(
                    action=action,
                    confidence=confidence,
                    reasoning=f"{reason} (cached)",
                )
            self._cache.pop(fingerprint, None)

        decision_dict = self._call_triage(alert)
        action = decision_dict.get("action", "investigate")
        if action not in _TRIAGE_VALID_ACTIONS:
            logger.warning(
                "Triage returned invalid action %r for alert %s; defaulting to investigate",
                action, alert.alert_id,
            )
            action = "investigate"
        reason = str(decision_dict.get("reason", ""))[:280]
        try:
            confidence = float(decision_dict.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))

        # Cache successful decisions only. Failed calls produce
        # action="investigate" with confidence=0 — don't cache those, so a
        # transient triage outage doesn't lock in "investigate everything"
        # for 5 minutes.
        if confidence > 0:
            self._cache[fingerprint] = (action, reason, confidence, _now_unix() + self._cache_ttl)

        return Decision(action=action, confidence=confidence, reasoning=reason)

    def _call_triage(self, alert: Alert) -> dict:
        """POST to the agent's /v1/triage. Returns the decision dict.

        On any error, returns a safe-default dict that decide() will
        translate into action="investigate" with confidence=0.
        """
        endpoint = f"{self._agent_url}/v1/triage"
        body = json.dumps(alert.to_dict(), default=str).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("triage response not a JSON object")
            return payload
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "Triage call failed for alert %s, defaulting to investigate: %s",
                alert.alert_id, exc,
            )
            return {"action": "investigate", "reason": f"triage call failed ({type(exc).__name__})", "confidence": 0.0}


def _now_unix() -> float:
    import time
    return time.time()


def build_http_triage_engine(agent_url: Optional[str]) -> Optional[HTTPTriageDecisionEngine]:
    """Construct the HTTP triage decision engine when an agent URL is configured.

    Returns None when ``agent_url`` is missing or empty, so the portable
    OpenReasoningDecisionEngine stays in place.
    """
    if not agent_url:
        return None
    return HTTPTriageDecisionEngine(agent_url=agent_url)
