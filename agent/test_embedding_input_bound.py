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
    MODEL_CONTEXT_TOKENS, MODEL_INPUT_CHAR_LIMITS, SPECIAL_TOKEN_ALLOWANCE,
    UNEMBEDDABLE_MEMORY, EmbeddingService, is_deterministic_input_error,
    max_input_chars,
)

#: The largest input the live nomic-embed-text endpoint accepted, measured per
#: character class at the boundary (CFOP-84). 2047 chars of ASCII punctuation,
#: CJK or Hebrew is rejected; 2046 chars of every class tried is accepted.
MEASURED_NOMIC_CEILING_CHARS = 2046

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
    """The absence of one was the first bug; a guessed one was the second.

    2046 is the model's 2048-token context less the two special tokens it
    wraps every input in. It is not a density estimate. The 4096 it replaces
    was one, and the endpoint rejected it on the very record the bound exists
    to rescue.
    """
    assert DEFAULT_EMBEDDING_MODEL in MODEL_CONTEXT_TOKENS
    assert max_input_chars(DEFAULT_EMBEDDING_MODEL) == 2046


def test_the_bound_never_exceeds_the_context_it_is_derived_from():
    """The guard that catches BOTH mistakes, for every model in the table.

    CFOP-81's version asserted the bound against a GUESSED density (2.5
    chars/token), so it passed at 4096 while the endpoint returned HTTP 500.
    There is no density here to get wrong: a token consumes at minimum one
    character, so N characters can never be more than N tokens, and the bound
    may not exceed the context less the special tokens.
    """
    assert MODEL_CONTEXT_TOKENS, "an empty table would make this vacuous"
    for model, context_tokens in MODEL_CONTEXT_TOKENS.items():
        limit = max_input_chars(model)
        assert limit + SPECIAL_TOKEN_ALLOWANCE <= context_tokens, (
            f"{model}: {limit} chars can be {limit} tokens, which with "
            f"{SPECIAL_TOKEN_ALLOWANCE} special tokens overflows a "
            f"{context_tokens}-token context")


def test_the_bound_agrees_with_what_the_endpoint_actually_accepted():
    """Arithmetic agreeing with itself is exactly what shipped the last bug.

    2046 is not reverse-engineered from the measurement; it falls out of
    2048 - 2. That the measured ceiling lands on the same number is the
    evidence the structural rule is the right rule, so raising the bound past
    it has to answer for itself.
    """
    assert max_input_chars(DEFAULT_EMBEDDING_MODEL) <= MEASURED_NOMIC_CEILING_CHARS


def test_a_tagged_model_name_resolves_to_the_same_bound():
    """``nomic-embed-text:latest`` is the same model.

    An exact-string lookup missed every tagged form and fell through to the
    unknown-model default, which sits far below what nomic can take. Since
    the default is now deliberately conservative, that miss is a silent 4x
    quality cut rather than a crash.
    """
    for tag in ("latest", "v1.5", "v1.5-q4"):
        assert (max_input_chars(f"{DEFAULT_EMBEDDING_MODEL}:{tag}")
                == max_input_chars(DEFAULT_EMBEDDING_MODEL))


def test_an_unknown_model_gets_a_conservative_default():
    """Guessing high for a new model reintroduces exactly this bug.

    CFOP-81's default was 2048 chars and called itself conservative. It was
    not: it sits ABOVE the 2046 the one model we have measured will accept,
    so an unnamed model with a smaller context inherited the same failure.
    """
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
    assert max_input_chars(DEFAULT_EMBEDDING_MODEL) == 2046


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


# ---------------------------------------------------------------------------
# the intersection that decides whether 2368 is indexed or merely quiet
# ---------------------------------------------------------------------------

def test_a_truncated_request_that_still_fails_is_not_retried_forever():
    """The production bug's exact shape, which neither prior test covered:
    oversized input, truncated, and the endpoint STILL says it does not fit.

    If the bound is too generous this is what happens after deploy — the loop
    goes quiet via the negative memory while the record stays unindexed. The
    loop must end, but so must the pretence that the row is fine.
    """
    svc = _service()
    text = "q" * (max_input_chars(svc.model) + 10_000)
    with patch("embedding_service.requests.post",
               return_value=_err_response(LIVE_ERROR)) as post:
        assert svc.generate_embedding(text) is None
        assert svc.generate_embedding(text) is None
    assert post.call_count == 1, "a truncated-and-still-too-long input was re-sent"
    assert svc._unembeddable_reason(text) is not None


# ---------------------------------------------------------------------------
# the metric is a mutually exclusive set (PR #171 review)
# ---------------------------------------------------------------------------

def _results(svc, text, response=None, side_effect=None):
    """Every result label recorded for one attempt."""
    seen = []
    with patch.object(EmbeddingService, "_record_result",
                      staticmethod(lambda r: seen.append(r))):
        kwargs = {"side_effect": side_effect} if side_effect else {"return_value": response}
        with patch("embedding_service.requests.post", **kwargs):
            svc.generate_embedding(text)
    return seen


def test_one_attempt_records_exactly_one_result():
    """`cfoperator_embedding_requests_total` is a REQUEST counter. The first
    version counted `truncated` on the way out and `success` on the way back,
    so every oversized record double-counted and read as both 'indexed
    faithfully' and 'not indexed faithfully'."""
    svc = _service()
    over = "r" * (max_input_chars(svc.model) + 500)
    assert _results(svc, over, _ok_response()) == ["truncated"]
    assert _results(_service(), "s" * 100, _ok_response()) == ["success"]


def test_the_decision_to_give_up_is_labelled_when_it_is_made():
    """`unembeddable` must fire on the call that decides the record will never
    be indexed — not on the next sweep. A restart in between would erase the
    event entirely, and a dashboard during the first pass would never see it.
    """
    assert _results(_service(), "t" * 100, _err_response(LIVE_ERROR)) == ["unembeddable"]


def test_a_retryable_failure_is_still_error():
    svc = _service()
    assert _results(svc, "u" * 100, _err_response('{"error":"model not found"}')) == ["error"]


def test_a_skipped_known_bad_input_records_unembeddable_once():
    svc = _service()
    _results(svc, "v" * 100, _err_response(LIVE_ERROR))          # first: decides
    assert _results(svc, "v" * 100, _ok_response()) == ["unembeddable"]  # second: skips
