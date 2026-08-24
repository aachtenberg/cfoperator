"""
Embedding Service for CFOperator

Generates text embeddings via Ollama's /api/embeddings endpoint.
Used for semantic search over past investigations.

Features:
- In-memory LRU cache with hash-based deduplication
- Database cache for cross-session persistence
- Batch indexing for coverage
"""
import hashlib
import json
import os
import requests
import threading
from collections import OrderedDict
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timezone

def _get_metrics():
    """Lazy import of Prometheus counters from agent.agent (avoids circular import)."""
    try:
        from agent.agent import EMBEDDING_REQUESTS, EMBEDDING_CACHE_HITS
        return EMBEDDING_REQUESTS, EMBEDDING_CACHE_HITS
    except ImportError:
        return None, None

# Default embedding model - nomic-embed-text is fast and high quality
DEFAULT_EMBEDDING_MODEL = "nomic-embed-text"
EMBEDDING_DIMENSION = 768  # nomic-embed-text dimension

# Upper bound on what we will send to the embedding endpoint (CFOP-81, CFOP-84).
#
# There was a MINIMUM input guard here and no maximum, so a single oversized
# record failed on every sweep for ~18 hours across three pod generations,
# burning a full HTTP round trip each time and leaving the index permanently
# one row behind. A head-truncated embedding is worth enormously more than no
# embedding: the row becomes searchable AND it leaves the unindexed set, which
# is what actually ends the loop.
#
# Measured in CHARACTERS, not tokens, deliberately: there is no tokenizer in
# this process and adding one to approximate a bound is a bad trade.
#
# CFOP-81 then picked the character bound by GUESSING A DENSITY -- 2
# chars/token, argued down from 3 because this corpus is JSON, paths, hex
# digests and stack traces and measured ~2.6 on a representative record. The
# guess was still too generous, and the guard test shipped alongside it
# encoded the same guess, so the test passed at 4096 while production kept
# returning HTTP 500 on the very record the fix existed to rescue. Only the
# NOISE stopped. That is the silent failure CFOP-81's own commit message
# predicted for a 3x ratio, and then walked into at 2x.
#
# So the bound is no longer a density guess. A token consumes AT MINIMUM one
# character, so N characters can never produce more than N tokens -- true for
# any text in any script, with no tokenizer and no corpus assumption. Subtract
# the special tokens the model wraps every input in ([CLS]/[SEP]) and the
# bound is exact rather than estimated.
#
# Measured against the live endpoint at the boundary, per character class:
#
#     input               2046      2047
#     ASCII punctuation   OK        EXCEEDS
#     CJK U+4E2D          OK        EXCEEDS
#     Hebrew U+05D0       OK        EXCEEDS
#     Samaritan U+0800    OK        OK
#     Linear-B U+10000    OK        OK
#     emoji U+1F600       OK        OK
#     space               OK        OK
#
# 2046 is exactly 2048 - 2, which is what the structural rule predicts, and
# the classes that tip over at 2047 are the ones that tokenize 1:1.
#
# A BYTE bound would not have worked either, and the same measurement says so:
# 2048 chars of ASCII punctuation is 2048 bytes and FAILS, while 2048 chars of
# emoji is 8192 bytes and PASSES. Bytes are no more predictive of tokens than
# characters are. Only the one-character-per-token floor is.
SPECIAL_TOKEN_ALLOWANCE = 2

#: Token context per model. This is the number to look up on a model swap: it
#: is a documented property of the model, unlike a density, which has to be
#: measured against a corpus and was measured wrong twice.
MODEL_CONTEXT_TOKENS = {
    "nomic-embed-text": 2048,
}

#: For a model we have no entry for. 512 tokens is the floor for the
#: BERT-family encoders these endpoints usually serve, so it is the honest
#: "we do not know" answer. CFOP-81 used 2048 here and called it conservative;
#: it was not, because it sits ABOVE the 2046 the one model we have actually
#: measured will accept. Truncating more than necessary costs some tail text;
#: truncating less than necessary costs the record. An operator who knows
#: better sets CFOP_EMBEDDING_MAX_CHARS.
DEFAULT_CONTEXT_TOKENS = 512


def _char_bound(context_tokens: int) -> int:
    """Characters that provably fit a ``context_tokens`` window."""
    return context_tokens - SPECIAL_TOKEN_ALLOWANCE


#: Derived views, kept because they read better at the call site than the
#: arithmetic does. The context table above is the single source of truth.
MODEL_INPUT_CHAR_LIMITS = {
    model: _char_bound(tokens) for model, tokens in MODEL_CONTEXT_TOKENS.items()
}
DEFAULT_MAX_INPUT_CHARS = _char_bound(DEFAULT_CONTEXT_TOKENS)

#: Bodies Ollama returns when the input cannot fit, whatever the status code.
#: This is a property of the INPUT, not of the moment, so a retry is
#: guaranteed waste — the distinction the old code could not draw, because a
#: timeout, a down endpoint and "this can never fit" all returned None alike.
DETERMINISTIC_INPUT_ERRORS = (
    "exceeds the context length",
    "input length exceeds",
)

#: How many known-unembeddable inputs to remember. Bounded because it is keyed
#: by content: an unbounded set is a slow leak on a long-lived agent.
UNEMBEDDABLE_MEMORY = 256


def _model_key(model: str) -> str:
    """The lookup key for ``model``, with any ``:tag`` suffix stripped.

    ``nomic-embed-text:latest`` is the same model as ``nomic-embed-text`` and
    is an ordinary thing to put in EMBEDDING_MODEL. Matching the exact string
    missed it and fell through to the unknown-model default -- which is the
    likeliest way this bug comes back, since the fallback is deliberately far
    below what nomic can actually take.
    """
    return str(model or "").split(":", 1)[0]


def max_input_chars(model: str) -> int:
    """Character bound for ``model``, overridable by CFOP_EMBEDDING_MAX_CHARS.

    The override exists so a model swap does not need a code change to be
    safe; a non-numeric or non-positive value is ignored rather than trusted.
    """
    raw = os.getenv("CFOP_EMBEDDING_MAX_CHARS", "").strip()
    if raw:
        try:
            override = int(raw)
            if override > 0:
                return override
        except ValueError:
            pass
    return MODEL_INPUT_CHAR_LIMITS.get(_model_key(model), DEFAULT_MAX_INPUT_CHARS)


def is_deterministic_input_error(body: str) -> bool:
    """True when the endpoint is telling us this input can never work."""
    text = str(body or "").lower()
    return any(marker in text for marker in DETERMINISTIC_INPUT_ERRORS)

# Cache settings
DEFAULT_CACHE_SIZE = 500  # Max embeddings to keep in memory
CACHE_TABLE_NAME = "embedding_cache"


def vector_literal(values: List[float]) -> str:
    """Render an embedding as a pgvector literal.

    The literal is string-interpolated into SQL (the ``::vector`` cast does not
    take a bind parameter), so every element is forced through ``float()``
    first: that way nothing but numbers can ever reach the statement, whatever
    the embedding endpoint returned.
    """
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def _log(level: str, msg: str, **fields: Any) -> None:
    """Structured logging matching agent pattern."""
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "component": "embedding_service",
        "msg": msg,
        **fields
    }
    print(json.dumps(payload, ensure_ascii=False))


class EmbeddingCache:
    """
    Two-tier embedding cache: in-memory LRU + database persistence.

    Uses MD5 hash of (model + text) as cache key for deduplication.
    This avoids re-computing embeddings for identical text.
    """

    def __init__(self, max_size: int = DEFAULT_CACHE_SIZE, db_session_factory=None):
        """
        Initialize cache.

        Args:
            max_size: Maximum entries in memory cache
            db_session_factory: Optional SQLAlchemy session factory for persistence
        """
        self._cache: OrderedDict[str, List[float]] = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()
        self._db_session_factory = db_session_factory
        self._stats = {'hits': 0, 'misses': 0, 'db_hits': 0}

    @staticmethod
    def compute_hash(text: str, model: str) -> str:
        """Compute cache key from text and model."""
        key_str = f"{model}:{text}"
        return hashlib.md5(key_str.encode('utf-8')).hexdigest()

    def get(self, text: str, model: str) -> Optional[List[float]]:
        """
        Get embedding from cache if available.

        Checks memory cache first, then database if configured.
        """
        key = self.compute_hash(text, model)

        with self._lock:
            # Check memory cache first
            if key in self._cache:
                # Move to end (most recently used)
                self._cache.move_to_end(key)
                self._stats['hits'] += 1
                return self._cache[key]

        # Check database cache if available
        if self._db_session_factory:
            embedding = self._get_from_db(key)
            if embedding:
                self._stats['db_hits'] += 1
                # Promote to memory cache
                self.put(text, model, embedding, persist=False)
                return embedding

        self._stats['misses'] += 1
        return None

    def put(self, text: str, model: str, embedding: List[float], persist: bool = True) -> None:
        """
        Store embedding in cache.

        Args:
            text: Original text
            model: Model used
            embedding: Embedding vector
            persist: Whether to also store in database (default True)
        """
        key = self.compute_hash(text, model)

        with self._lock:
            # Add to memory cache
            self._cache[key] = embedding
            self._cache.move_to_end(key)

            # Evict oldest if over capacity
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

        # Persist to database
        if persist and self._db_session_factory:
            self._put_to_db(key, model, embedding)

    def _get_from_db(self, key: str) -> Optional[List[float]]:
        """Fetch embedding from database cache."""
        try:
            from sqlalchemy import text
            with self._db_session_factory() as session:
                result = session.execute(text(f"""
                    SELECT embedding FROM {CACHE_TABLE_NAME}
                    WHERE hash_key = :key
                """), {'key': key}).fetchone()
                if result and result[0]:
                    # Parse JSON array or pgvector format
                    embedding = result[0]
                    if isinstance(embedding, str):
                        # Handle pgvector string format [0.1, 0.2, ...]
                        embedding = json.loads(embedding.replace('[', '[').replace(']', ']'))
                    return list(embedding)
        except Exception as e:
            _log("debug", "DB cache lookup failed", error=str(e))
        return None

    def _put_to_db(self, key: str, model: str, embedding: List[float]) -> None:
        """Store embedding in database cache."""
        try:
            from sqlalchemy import text
            embedding_str = vector_literal(embedding)
            with self._db_session_factory() as session:
                session.execute(text(f"""
                    INSERT INTO {CACHE_TABLE_NAME} (hash_key, embedding_model, embedding, created_at)
                    VALUES (:key, :model, :embedding, NOW())
                    ON CONFLICT (hash_key) DO UPDATE SET
                        embedding = EXCLUDED.embedding,
                        created_at = NOW()
                """), {
                    'key': key,
                    'model': model,
                    'embedding': embedding_str
                })
                session.commit()
        except Exception as e:
            _log("debug", "DB cache store failed", error=str(e))

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            return {
                'memory_size': len(self._cache),
                'max_size': self._max_size,
                **self._stats
            }

    def clear(self) -> None:
        """Clear memory cache."""
        with self._lock:
            self._cache.clear()


class EmbeddingService:
    """
    Generate embeddings via Ollama's native API.

    Uses Ollama's /api/embeddings endpoint (not OpenAI-compatible).
    Falls back gracefully if Ollama is unavailable.

    Features:
    - Two-tier caching (memory + database)
    - Hash-based deduplication
    - Batch indexing support
    """

    def __init__(
        self,
        ollama_url: Optional[str] = None,
        model: Optional[str] = None,
        cache_size: int = DEFAULT_CACHE_SIZE,
        db_session_factory=None
    ):
        """
        Initialize embedding service.

        Args:
            ollama_url: Ollama base URL (e.g., http://localhost:11434)
            model: Embedding model name (default: nomic-embed-text)
            cache_size: Maximum embeddings to cache in memory
            db_session_factory: Optional SQLAlchemy session factory for persistent cache
        """
        self.ollama_url = ollama_url or os.getenv("OLLAMA_URL", "")
        self.model = model or os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
        self._available: Optional[bool] = None  # Lazy check
        self._cache = EmbeddingCache(max_size=cache_size, db_session_factory=db_session_factory)
        # Inputs the endpoint has told us can never be embedded (CFOP-81).
        # The positive cache only ever recorded SUCCESSES, so nothing
        # remembered a deterministic failure and every sweep paid for it
        # again. In-memory and not persisted on purpose: a restart should
        # retry, because the thing that changed might be the model config.
        self._unembeddable: "OrderedDict[str, str]" = OrderedDict()
        self._unembeddable_lock = threading.Lock()

    def _mark_unembeddable(self, text: str, reason: str) -> None:
        key = EmbeddingCache.compute_hash(text, self.model)
        with self._unembeddable_lock:
            if key in self._unembeddable:
                return
            self._unembeddable[key] = reason
            while len(self._unembeddable) > UNEMBEDDABLE_MEMORY:
                self._unembeddable.popitem(last=False)
        _log("warn", "Input can never be embedded; not retrying it",
             model=self.model, text_len=len(text), reason=reason[:200])

    @staticmethod
    def _record_result(result: str) -> None:
        """Record exactly one outcome for one embedding attempt.

        Centralised because the labels are a mutually exclusive set and the
        first version was not: it counted `truncated` on the way out and
        `success` on the way back, double-counting every oversized record.
        """
        _er, _ec = _get_metrics()
        if _er:
            _er.labels(result=result).inc()

    def _unembeddable_reason(self, text: str) -> Optional[str]:
        key = EmbeddingCache.compute_hash(text, self.model)
        with self._unembeddable_lock:
            return self._unembeddable.get(key)

    def is_available(self) -> bool:
        """Check if embedding service is available."""
        if self._available is not None:
            return self._available

        if not self.ollama_url:
            self._available = False
            return False

        try:
            # Quick health check
            response = requests.get(
                f"{self.ollama_url}/api/tags",
                timeout=5
            )
            self._available = response.status_code == 200
        except Exception as e:
            _log("warn", "Ollama not available for embeddings", error=str(e))
            self._available = False

        return self._available

    def reset_availability(self) -> None:
        """Reset availability check to re-probe on next call."""
        self._available = None

    def generate_embedding(self, text: str, use_cache: bool = True) -> Optional[List[float]]:
        """
        Generate embedding for text using Ollama with caching.

        Args:
            text: Text to embed
            use_cache: Whether to use cache (default True)

        Returns:
            List of floats (embedding vector) or None if failed
        """
        if not text or len(text.strip()) < 5:
            _log("debug", "Text too short for embedding", text_len=len(text) if text else 0)
            return None

        # Check cache first
        if use_cache:
            cached = self._cache.get(text, self.model)
            if cached:
                _log("debug", "Embedding cache hit",
                     model=self.model,
                     text_len=len(text))
                _er, _ec = _get_metrics()
                if _ec:
                    _ec.labels(result='hit').inc()
                return cached
            else:
                _er, _ec = _get_metrics()
                if _ec:
                    _ec.labels(result='miss').inc()

        # Known-bad input: refuse before spending a round trip. Checked after
        # the positive cache so a value that later becomes embeddable (a
        # restart with a different model clears this) is not shadowed.
        known_bad = self._unembeddable_reason(text)
        if known_bad:
            _log("debug", "Skipping known-unembeddable input",
                 model=self.model, text_len=len(text), reason=known_bad[:120])
            self._record_result('unembeddable')
            return None

        # Need to generate - check availability
        if not self.is_available():
            return None

        # Bound the input (CFOP-81). Head-truncated is worth far more than
        # skipped: the record becomes searchable and leaves the unindexed set,
        # which is what ends the retry loop. Cached under the ORIGINAL text so
        # the next caller passing the same oversized record gets a cache hit
        # rather than re-truncating and re-sending it.
        limit = max_input_chars(self.model)
        prompt = text
        truncated = len(prompt) > limit
        if truncated:
            prompt = prompt[:limit]
            _log("warn", "Input truncated to fit the embedding model",
                 model=self.model, original_len=len(text), sent_len=len(prompt),
                 limit=limit)

        try:
            response = requests.post(
                f"{self.ollama_url}/api/embeddings",
                json={
                    "model": self.model,
                    "prompt": prompt
                },
                timeout=30
            )

            if response.status_code == 200:
                data = response.json()
                embedding = data.get("embedding")
                if embedding and len(embedding) > 0:
                    # Cache the result
                    if use_cache:
                        self._cache.put(text, self.model, embedding)
                    _log("debug", "Embedding generated",
                         model=self.model,
                         text_len=len(text),
                         embedding_dim=len(embedding))
                    # ONE result per attempt: a truncated embed that lands is
                    # `truncated`, not truncated+success. Counting both would
                    # double the request total for every oversized record and
                    # make the same attempt read as "indexed faithfully" and
                    # "not indexed faithfully" at once.
                    self._record_result('truncated' if truncated else 'success')
                    return embedding

            body = response.text or ""
            # A failure the endpoint attributes to the INPUT is not going to
            # go away on the next sweep, so remember it rather than paying for
            # it forever. Everything else — a timeout, a 500 from an
            # overloaded endpoint, a missing model — stays retryable, because
            # those genuinely do get better on their own.
            if is_deterministic_input_error(body):
                self._mark_unembeddable(
                    text, f"HTTP {response.status_code}: {body[:160]}")
                # Labelled on THIS call, not the next one. This is the attempt
                # that decided the record will never be indexed; labelling it
                # `error` would call the decision retryable and hide the event
                # until a second sweep — which a restart in between erases
                # entirely.
                self._record_result('unembeddable')
            else:
                _log("warn", "Failed to generate embedding",
                     status=response.status_code,
                     response=body[:200])
                self._record_result('error')
            return None

        except requests.exceptions.Timeout:
            _log("warn", "Embedding generation timed out", model=self.model)
            self._record_result('error')
            return None
        except Exception as e:
            _log("error", "Embedding generation error", error=str(e))
            self._record_result('error')
            return None

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get embedding cache statistics."""
        return self._cache.get_stats()

    def clear_cache(self) -> None:
        """Clear the embedding cache."""
        self._cache.clear()

    def create_investigation_text(self, investigation: Dict[str, Any]) -> str:
        """
        Create embeddable text from investigation data.

        Combines trigger, findings summary, and key learnings into a
        single text optimized for semantic similarity.

        Args:
            investigation: Dict with trigger, findings, outcome keys

        Returns:
            Combined text suitable for embedding
        """
        parts = []

        # Trigger (the problem that was investigated)
        trigger = investigation.get("trigger", "")
        if trigger:
            parts.append(f"Issue: {trigger}")

        # Findings
        findings = investigation.get("findings", {})
        if isinstance(findings, dict):
            # Summary or hypothesis
            summary = findings.get("summary", "") or findings.get("hypothesis", "")
            if summary:
                parts.append(f"Summary: {summary}")

            # Evidence
            evidence = findings.get("evidence", "")
            if evidence:
                if isinstance(evidence, list):
                    evidence = "; ".join(str(e) for e in evidence[:5])
                parts.append(f"Evidence: {evidence}")

            # Learnings (key insights)
            learnings = findings.get("learnings", "")
            if learnings:
                if isinstance(learnings, list):
                    learnings = "; ".join(str(l) for l in learnings[:5])
                parts.append(f"Learnings: {learnings}")

            # Actions taken
            actions = findings.get("actions_taken", []) or findings.get("actions", [])
            if actions:
                if isinstance(actions, list):
                    actions = "; ".join(str(a) for a in actions[:5])
                parts.append(f"Actions: {actions}")

        elif isinstance(findings, str) and findings:
            # Sometimes findings is just a string
            parts.append(f"Findings: {findings}")

        # Outcome
        outcome = investigation.get("outcome", "")
        if outcome:
            parts.append(f"Outcome: {outcome}")

        return "\n".join(parts)

    def batch_index_investigations(
        self,
        kb,
        batch_size: int = 10,
        max_total: int = 50
    ) -> Dict[str, Any]:
        """
        Batch index unindexed investigations.

        Processes investigations that don't have embeddings yet in batches.
        Suitable for running as a scheduled job or on-demand.

        Args:
            kb: KnowledgeBase instance
            batch_size: Number to process per batch (with pause between)
            max_total: Maximum total to process in one run

        Returns:
            Dict with stats: processed, success, failed, remaining
        """
        if not self.is_available():
            return {"error": "Embedding service not available", "processed": 0}

        # Ensure FTS schema exists
        kb.ensure_fts_schema()

        # Get unindexed investigations
        unindexed = kb.get_unindexed_investigations(limit=max_total)
        if not unindexed:
            return {"processed": 0, "success": 0, "failed": 0, "remaining": 0}

        _log("info", "Starting batch indexing",
             unindexed_count=len(unindexed),
             batch_size=batch_size)

        processed = 0
        success = 0
        failed = 0

        for investigation in unindexed:
            inv_id = investigation['id']

            # Create embeddable text
            embedding_text = self.create_investigation_text(investigation)
            if not embedding_text or len(embedding_text) < 10:
                _log("debug", "Skipping investigation - text too short", investigation_id=inv_id)
                processed += 1
                continue

            # Generate embedding
            embedding = self.generate_embedding(embedding_text)
            if not embedding:
                _log("warn", "Failed to generate embedding", investigation_id=inv_id)
                failed += 1
                processed += 1
                continue

            # Store embedding (also updates FTS vector)
            stored = kb.store_investigation_embedding(
                investigation_id=inv_id,
                embedding=embedding,
                embedding_model=self.model,
                embedding_text=embedding_text
            )

            if stored:
                success += 1
            else:
                failed += 1

            processed += 1

            # Pause every batch_size to avoid overloading Ollama
            if processed % batch_size == 0:
                import time
                time.sleep(0.5)

        # Check remaining
        remaining = len(kb.get_unindexed_investigations(limit=1))

        _log("info", "Batch indexing complete",
             processed=processed,
             success=success,
             failed=failed,
             remaining=remaining)

        return {
            "processed": processed,
            "success": success,
            "failed": failed,
            "remaining": remaining
        }

    def batch_index_learnings(
        self,
        kb,
        batch_size: int = 10,
        max_total: int = 50
    ) -> Dict[str, Any]:
        """
        Batch index unindexed learnings.

        Processes learnings that don't have embeddings yet.
        """
        if not self.is_available():
            return {"error": "Embedding service not available", "processed": 0}

        unindexed = kb.get_unindexed_learnings(limit=max_total)
        if not unindexed:
            return {"processed": 0, "success": 0, "failed": 0, "remaining": 0}

        _log("info", "Starting learning batch indexing",
             unindexed_count=len(unindexed),
             batch_size=batch_size)

        processed = 0
        success = 0
        failed = 0

        for learning in unindexed:
            lid = learning['id']

            # Build search text from learning fields
            search_text = ' '.join(filter(None, [
                learning.get('title', ''),
                learning.get('description', ''),
                learning.get('applies_when', ''),
            ]))

            if not search_text or len(search_text) < 10:
                processed += 1
                continue

            embedding = self.generate_embedding(search_text)
            if not embedding:
                _log("warn", "Failed to generate learning embedding", learning_id=lid)
                failed += 1
                processed += 1
                continue

            # Store embedding
            try:
                import hashlib
                from sqlalchemy import text as sql_text
                embedding_str = vector_literal(embedding)
                with kb.session_scope() as session:
                    session.execute(sql_text("""
                        UPDATE investigation_learnings
                        SET embedding_hash = :hash
                        WHERE id = :lid
                    """), {'hash': hashlib.md5(search_text.encode()).hexdigest(), 'lid': lid})
                    session.execute(sql_text("""
                        INSERT INTO learning_embeddings (learning_id, embedding, embedding_model, embedding_text)
                        VALUES (:lid, :embedding, :model, :text)
                        ON CONFLICT (learning_id) DO UPDATE SET
                            embedding = EXCLUDED.embedding,
                            embedding_model = EXCLUDED.embedding_model,
                            embedding_text = EXCLUDED.embedding_text
                    """), {
                        'lid': lid,
                        'embedding': embedding_str,
                        'model': self.model,
                        'text': search_text
                    })
                    session.commit()
                success += 1
                _log("info", "Learning embedding stored", learning_id=lid)
            except Exception as e:
                _log("warn", "Failed to store learning embedding", learning_id=lid, error=str(e))
                failed += 1

            processed += 1

            if processed % batch_size == 0:
                import time
                time.sleep(0.5)

        remaining = len(kb.get_unindexed_learnings(limit=1))

        _log("info", "Learning batch indexing complete",
             processed=processed, success=success,
             failed=failed, remaining=remaining)

        return {
            "processed": processed,
            "success": success,
            "failed": failed,
            "remaining": remaining
        }
