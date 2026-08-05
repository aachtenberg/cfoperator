"""Tests for the in-OODA LLM fallback chain.

The original chat handler iterated the provider chain on failure, but OODA
paths (sweep, investigation, morning summary, verification) called
_chat_with_tools once with a single provider — so a transient Ollama
timeout (e.g. cold-start after the GPU unloaded the model) would abort the
whole operation. _chat_with_tools_with_fallback fixes that: any exception
from one provider records a failure for cooldown, logs it, and tries the
next provider in the chain. These tests pin that behavior.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator
from llm_fallback import LLMFallbackManager


def _operator(provider_chain, chat_responses):
    """Build a CFOperator with the bare minimum mocked to drive the helper.

    Args:
        provider_chain: list of (provider_type, url, model) tuples the helper
                        will see from _get_provider_chain.
        chat_responses: iterable of either:
                        - dict result (the helper returns this)
                        - Exception instance (the helper raises this for that
                          attempt and falls through to the next provider).
    """
    op = CFOperator.__new__(CFOperator)
    op.llm = MagicMock()
    op._get_provider_chain = lambda backend='auto', model=None: list(provider_chain)
    iterator = iter(chat_responses)

    def fake_chat_with_tools(provider_type, url, model, messages, system_context,
                             max_iterations=None, event_callback=None):
        item = next(iterator)
        if isinstance(item, Exception):
            raise item
        return item

    op._chat_with_tools = fake_chat_with_tools
    return op


# ---- happy path --------------------------------------------------------------


def test_primary_provider_succeeds_returns_result_and_no_fallback():
    op = _operator(
        provider_chain=[("ollama", "http://localhost:11434", "qwen3:14b")],
        chat_responses=[{"response": "ok", "tool_calls": 3}],
    )
    result = op._chat_with_tools_with_fallback(messages=[{"role": "user", "content": "x"}])
    assert result["response"] == "ok"
    assert result["tool_calls"] == 3
    assert result["backend"] == "ollama"
    assert result["model"] == "qwen3:14b"
    assert result["fallback_used"] is False
    # success was recorded with the right provider key
    op.llm.record_success.assert_called_once_with("ollama/http://localhost:11434/qwen3:14b")
    op.llm.record_failure.assert_not_called()


# ---- fall-through on failure -------------------------------------------------


def test_falls_over_to_next_provider_on_exception():
    op = _operator(
        provider_chain=[
            ("ollama", "http://localhost:11434", "qwen3:14b"),
            ("groq",   None, "llama-3.3-70b"),
        ],
        chat_responses=[
            TimeoutError("Ollama read timeout"),  # primary times out
            {"response": "groq says hi", "tool_calls": 1},  # fallback succeeds
        ],
    )
    result = op._chat_with_tools_with_fallback(messages=[{"role": "user", "content": "x"}])
    assert result["response"] == "groq says hi"
    assert result["backend"] == "groq"
    assert result["model"] == "llama-3.3-70b"
    assert result["fallback_used"] is True
    op.llm.record_failure.assert_called_once()
    op.llm.record_success.assert_called_once_with("groq/llama-3.3-70b")


def test_event_callback_fires_with_fallback_event_when_falling_through():
    seen = []
    op = _operator(
        provider_chain=[
            ("ollama", "http://localhost:11434", "qwen3:14b"),
            ("groq",   None, "llama-3.3-70b"),
        ],
        chat_responses=[
            RuntimeError("connection refused"),
            {"response": "ok", "tool_calls": 0},
        ],
    )
    op._chat_with_tools_with_fallback(
        messages=[{"role": "user", "content": "x"}],
        event_callback=lambda kind, payload: seen.append((kind, payload)),
    )
    fallback_events = [e for e in seen if e[0] == "fallback"]
    assert len(fallback_events) == 1
    payload = fallback_events[0][1]
    assert payload["from"] == "ollama/qwen3:14b"
    assert payload["to"] == "groq/llama-3.3-70b"
    assert "connection refused" in payload["reason"]


def test_failure_recorded_with_classified_reason_not_raw_string():
    """record_failure must receive classify_error(exc)'s short bucket, not
    str(exc): the DB column and cooldown logic key off the reason, and a raw
    exception string is exactly what caused the StringDataRightTruncation this
    PR fixes. Pinning the argument stops a silent revert to str(e)."""
    boom = TimeoutError("Ollama read timeout")
    op = _operator(
        provider_chain=[
            ("ollama", "http://localhost:11434", "qwen3:14b"),
            ("groq", None, "llama-3.3-70b"),
        ],
        chat_responses=[boom, {"response": "ok", "tool_calls": 0}],
    )
    op._chat_with_tools_with_fallback(messages=[{"role": "user", "content": "x"}])

    op.llm.classify_error.assert_called_once_with(boom)
    op.llm.record_failure.assert_called_once_with(
        "ollama/http://localhost:11434/qwen3:14b",
        op.llm.classify_error.return_value,
    )


# ---- all providers exhausted -------------------------------------------------


def test_all_providers_failing_raises_last_error():
    last_err = ValueError("groq is down")
    op = _operator(
        provider_chain=[
            ("ollama", "http://localhost:11434", "qwen3:14b"),
            ("groq",   None, "llama-3.3-70b"),
        ],
        chat_responses=[
            TimeoutError("Ollama read timeout"),
            last_err,
        ],
    )
    with pytest.raises(ValueError, match="groq is down"):
        op._chat_with_tools_with_fallback(messages=[{"role": "user", "content": "x"}])
    # both failures recorded for cooldown
    assert op.llm.record_failure.call_count == 2
    op.llm.record_success.assert_not_called()


def test_empty_chain_raises_runtime_error_with_specific_message():
    op = _operator(provider_chain=[], chat_responses=[])
    with pytest.raises(RuntimeError, match="No LLM providers available"):
        op._chat_with_tools_with_fallback(messages=[{"role": "user", "content": "x"}])


# ---- argument plumbing -------------------------------------------------------


def test_passes_max_iterations_and_system_context_through():
    captured = {}
    op = _operator(provider_chain=[("ollama", None, "m")], chat_responses=[{"response": "", "tool_calls": 0}])
    original = op._chat_with_tools

    def spy_chat(provider_type, url, model, messages, system_context, max_iterations=None, event_callback=None):
        captured["max_iterations"] = max_iterations
        captured["system_context"] = system_context
        captured["messages"] = messages
        return original(provider_type, url, model, messages, system_context, max_iterations, event_callback)

    op._chat_with_tools = spy_chat
    op._chat_with_tools_with_fallback(
        messages=[{"role": "user", "content": "investigate this"}],
        system_context="You are CFOperator.",
        max_iterations=15,
    )
    assert captured["max_iterations"] == 15
    assert captured["system_context"] == "You are CFOperator."
    assert captured["messages"] == [{"role": "user", "content": "investigate this"}]


# ---- error classification ----------------------------------------------------


def _manager():
    """LLMFallbackManager without touching the DB (skips __init__/_ensure_table);
    classify_error/calculate_cooldown only read class-level pattern lists."""
    return LLMFallbackManager.__new__(LLMFallbackManager)


@pytest.mark.parametrize("exc,status,expected", [
    # 4xx / malformed request: must NOT be a transient "connection" error.
    (Exception("400 Bad Request"), None, "bad_request"),
    (Exception("invalid_request_error: messages: text content blocks..."), None, "bad_request"),
    (Exception("Error code: 422 Unprocessable Entity"), None, "bad_request"),
    (Exception("boom"), 400, "bad_request"),
    (Exception("boom"), 404, "bad_request"),
    # Regression guards: existing buckets unchanged.
    (Exception("rate limit exceeded"), None, "rate_limit"),
    (Exception("boom"), 429, "rate_limit"),
    (Exception("unauthorized"), None, "auth"),
    (Exception("boom"), 401, "auth"),
    (Exception("read timed out"), None, "timeout"),
    (Exception("some unknown failure"), None, "connection"),
])
def test_classify_error_buckets(exc, status, expected):
    assert _manager().classify_error(exc, status) == expected


def test_bad_request_gets_short_cooldown_not_exponential():
    """A persistent malformed-request bug must not sideline a healthy provider
    for an hour: bad_request gets the same short fixed cooldown as auth, while a
    genuine transient error still escalates."""
    m = _manager()
    from datetime import timedelta
    assert m.calculate_cooldown(4, "bad_request") == timedelta(minutes=5)
    assert m.calculate_cooldown(4, "auth") == timedelta(minutes=5)
    assert m.calculate_cooldown(4, "connection") == timedelta(minutes=60)
