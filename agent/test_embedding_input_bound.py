#!/usr/bin/env python3
"""The embedding service's input bound, and the retry loop it ends (CFOP-81).

`generate_embedding` refused text under 5 characters and had no upper bound at
all, so one oversized record (learning 2368) failed on every sweep for ~18
hours across three pod generations. Three things compounded:

  1. no maximum, so the request could never succeed;
  2. a permanent failure was indistinguishable from a transient one — a
     timeout, a down endpoint and "this can never fit" all returned None;
  3. failure means the row is never stored, and the unindexed set is
     recomputed each pass, so the same record was picked again forever, at the
     cost of a full HTTP round trip each time.

These cover the bound, the classification, and the negative memory that makes
(3) cheap even for a failure truncation cannot fix.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from embedding_service import (  # noqa: E402
    DEFAULT_EMBEDDING_MODEL, DEFAULT_MAX_INPUT_CHARS, EMBEDDING_DIMENSION,
    MODEL_INPUT_CHAR_LIMITS, UNEMBEDDABLE_MEMORY, EmbeddingService,
    is_deterministic_input_error, max_input_chars,
)

#: The body Ollama actually returned on learning 2368, verbatim.
LIVE_ERROR = '{"error":"the input length exceeds the context length"}'


def _service():
    svc = EmbeddingService(ollama_url="http://ollama:11434")
    svc._available = True          # skip the availability probe
    return svc


def _ok_response(dim=EMBEDDING_DIMENSION):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"embedding": [0.1] * dim}
    r.text = ""
    return r


def _err_response(body, status=500):
    r = MagicMock()
    r.status_code = status
    r.text = body
    r.json.return_value = {}
    return r


# ---------------------------------------------------------------------------
# the bound
# ---------------------------------------------------------------------------

def test_the_default_model_has_an_explicit_limit():
    """The absence of one is the bug. nomic-embed-text holds 2048 tokens, and
    this text is logs and JSON, which tokenize denser than prose."""
    assert DEFAULT_EMBEDDING_MODEL in MODEL_INPUT_CHAR_LIMITS
    assert max_input_chars(DEFAULT_EMBEDDING_MODEL) == 2048 * 3


def test_an_unknown_model_gets_a_conservative_default():
    """Guessing high for a new model reintroduces exactly this bug."""
    assert max_input_chars("some-future-model") == DEFAULT_MAX_INPUT_CHARS
    assert DEFAULT_MAX_INPUT_CHARS < max_input_chars(DEFAULT_EMBEDDING_MODEL)


def test_the_limit_is_overridable_without_a_code_change(monkeypatch):
    monkeypatch.setenv("CFOP_EMBEDDING_MAX_CHARS", "999")
    assert max_input_chars(DEFAULT_EMBEDDING_MODEL) == 999


@pytest.mark.parametrize("bad", ["0", "-1", "lots", "", "  "])
def test_a_junk_override_is_ignored_rather_than_trusted(monkeypatch, bad):
    """A zero or negative bound would truncate every input to nothing and
    quietly destroy the index."""
    monkeypatch.setenv("CFOP_EMBEDDING_MAX_CHARS", bad)
    assert max_input_chars(DEFAULT_EMBEDDING_MODEL) == 2048 * 3


# ---------------------------------------------------------------------------
# truncation
# ---------------------------------------------------------------------------

def test_an_oversized_record_is_embedded_from_its_head_not_skipped():
    """The whole point: it becomes searchable AND leaves the unindexed set,
    which is what actually ends the loop."""
    svc = _service()
    limit = max_input_chars(svc.model)
    text = "x" * (limit + 5000)
    with patch("embedding_service.requests.post", return_value=_ok_response()) as post:
        result = svc.generate_embedding(text)
    assert result is not None and len(result) == EMBEDDING_DIMENSION
    sent = post.call_args.kwargs["json"]["prompt"]
    assert len(sent) == limit, "the request was not bounded"


def test_a_record_within_the_limit_is_sent_untouched():
    svc = _service()
    text = "y" * 100
    with patch("embedding_service.requests.post", return_value=_ok_response()) as post:
        svc.generate_embedding(text)
    assert post.call_args.kwargs["json"]["prompt"] == text


def test_the_result_is_cached_under_the_original_text():
    """Not under the truncated payload: the next caller passes the same
    oversized record, and must get a cache hit rather than re-truncating and
    re-sending it."""
    svc = _service()
    text = "z" * (max_input_chars(svc.model) + 1000)
    with patch("embedding_service.requests.post", return_value=_ok_response()) as post:
        svc.generate_embedding(text)
        svc.generate_embedding(text)
    assert post.call_count == 1, "the second call re-sent an already-embedded record"


# ---------------------------------------------------------------------------
# transient vs permanent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body,expected", [
    (LIVE_ERROR, True),
    ('{"error":"input length exceeds 2048"}', True),
    ('{"error":"THE INPUT LENGTH EXCEEDS THE CONTEXT LENGTH"}', True),
    ('{"error":"model not found"}', False),
    ('{"error":"connection reset"}', False),
    ("", False),
    (None, False),
])
def test_only_input_attributed_failures_are_deterministic(body, expected):
    """A timeout, a down endpoint and a missing model all get better on their
    own. "This input cannot fit" does not."""
    assert is_deterministic_input_error(body) is expected


def test_a_deterministic_failure_is_not_retried():
    """The loop, ended. Second call must cost nothing."""
    svc = _service()
    with patch("embedding_service.requests.post",
               return_value=_err_response(LIVE_ERROR)) as post:
        assert svc.generate_embedding("a" * 50) is None
        assert svc.generate_embedding("a" * 50) is None
    assert post.call_count == 1, "a known-unembeddable input was re-sent"


def test_a_transient_failure_is_retried():
    """Poisoning on a blip would be worse than the bug: a down Ollama would
    permanently orphan every record it touched."""
    svc = _service()
    with patch("embedding_service.requests.post",
               return_value=_err_response('{"error":"model not found"}')) as post:
        svc.generate_embedding("b" * 50)
        svc.generate_embedding("b" * 50)
    assert post.call_count == 2


def test_a_timeout_is_retried():
    import requests as _requests
    svc = _service()
    with patch("embedding_service.requests.post",
               side_effect=_requests.exceptions.Timeout()) as post:
        svc.generate_embedding("c" * 50)
        svc.generate_embedding("c" * 50)
    assert post.call_count == 2


def test_different_records_are_tracked_independently():
    """One bad record must not silence its neighbours."""
    svc = _service()
    with patch("embedding_service.requests.post",
               return_value=_err_response(LIVE_ERROR)):
        svc.generate_embedding("bad-one" * 10)
    with patch("embedding_service.requests.post", return_value=_ok_response()) as post:
        assert svc.generate_embedding("good-one" * 10) is not None
    assert post.call_count == 1


def test_the_negative_memory_is_bounded():
    """Keyed by content, so unbounded is a slow leak on an agent that runs for
    months."""
    svc = _service()
    with patch("embedding_service.requests.post",
               return_value=_err_response(LIVE_ERROR)):
        for i in range(UNEMBEDDABLE_MEMORY + 40):
            svc.generate_embedding(f"record-{i}-{'p' * 20}")
    assert len(svc._unembeddable) <= UNEMBEDDABLE_MEMORY


def test_the_negative_memory_is_not_persisted():
    """A restart should retry: the thing that changed might be the model
    config, and a durable tombstone would outlive the fix."""
    svc = _service()
    with patch("embedding_service.requests.post",
               return_value=_err_response(LIVE_ERROR)):
        svc.generate_embedding("d" * 50)
    fresh = _service()
    with patch("embedding_service.requests.post", return_value=_ok_response()) as post:
        assert fresh.generate_embedding("d" * 50) is not None
    assert post.call_count == 1


def test_the_short_input_guard_still_holds():
    """The minimum was never the bug; it must survive the maximum landing."""
    svc = _service()
    with patch("embedding_service.requests.post") as post:
        assert svc.generate_embedding("hi") is None
    post.assert_not_called()
