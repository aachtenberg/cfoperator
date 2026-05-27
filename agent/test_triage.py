"""Tests for the agent-side triage classifier.

run_triage classifies an alert into log_only / notify / investigate /
escalate by asking the LLM with a one-shot prompt. The decision engine on
the event_runtime side will use whatever this returns.

Tests cover:
  - _parse_triage_response across the LLM's actual messy output shapes
    (raw JSON, fenced JSON, JSON with surrounding prose, garbage)
  - run_triage's contract: returns a valid decision dict even when the
    LLM is unavailable, returns garbage, or returns an unknown action
  - Tolerates missing embedding service (no similar context available)
"""

from __future__ import annotations

import os
import sys
import threading as _t
from queue import Queue
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator


def _operator():
    op = CFOperator.__new__(CFOperator)
    op.config = {}
    # Stub embeddings: defaults to "not available" so we don't try to do
    # similarity search in unit tests. Individual tests override as needed.
    op.embeddings = MagicMock()
    op.embeddings.is_available.return_value = False
    op.kb = MagicMock()
    op._investigation_lock = _t.Lock()
    op._investigation_queue = Queue(maxsize=8)
    return op


# ---- _parse_triage_response: tolerate messy LLM output -------------------


def test_parse_raw_json():
    d = CFOperator._parse_triage_response(
        '{"action": "notify", "reason": "known noise", "confidence": 0.9}'
    )
    assert d == {"action": "notify", "reason": "known noise", "confidence": 0.9}


def test_parse_fenced_json():
    d = CFOperator._parse_triage_response(
        '```json\n{"action": "investigate", "reason": "novel pattern", "confidence": 0.8}\n```'
    )
    assert d is not None
    assert d["action"] == "investigate"
    assert d["confidence"] == 0.8


def test_parse_json_with_surrounding_prose():
    d = CFOperator._parse_triage_response(
        'Here is my classification:\n'
        '{"action": "log_only", "reason": "test pod", "confidence": 0.95}\n'
        'Hope that helps!'
    )
    assert d is not None
    assert d["action"] == "log_only"


def test_parse_unknown_action_returns_none():
    """LLM hallucinated a non-rubric action. Caller should default to investigate."""
    d = CFOperator._parse_triage_response('{"action": "ponder", "reason": "...", "confidence": 0.5}')
    assert d is None


def test_parse_no_action_field_returns_none():
    d = CFOperator._parse_triage_response('{"reason": "looks bad", "confidence": 0.5}')
    assert d is None


def test_parse_garbage_returns_none():
    assert CFOperator._parse_triage_response('') is None
    assert CFOperator._parse_triage_response('lol no json here') is None
    assert CFOperator._parse_triage_response('{not valid json') is None


def test_parse_missing_confidence_defaults_to_0_5():
    d = CFOperator._parse_triage_response('{"action": "notify", "reason": "ok"}')
    assert d is not None
    assert d["confidence"] == 0.5


def test_parse_caps_reason_length():
    long_reason = "x" * 500
    d = CFOperator._parse_triage_response(f'{{"action": "notify", "reason": "{long_reason}"}}')
    assert d is not None
    assert len(d["reason"]) <= 280


# ---- run_triage: contract behavior ---------------------------------------


def _alert(severity="warning", summary="Pod foo not ready", **labels):
    return {
        "severity": severity,
        "summary": summary,
        "labels": labels,
        "details": {},
        "alert_id": "test-alert-1",
    }


def test_run_triage_happy_path_returns_llm_decision():
    op = _operator()
    op._chat_with_tools_with_fallback = MagicMock(return_value={
        "response": '{"action": "notify", "reason": "kubelet self-heals", "confidence": 0.8}',
        "tool_calls": 0,
    })
    result = op.run_triage(_alert())
    assert result == {"action": "notify", "reason": "kubelet self-heals", "confidence": 0.8}


def test_run_triage_falls_back_to_investigate_on_llm_failure():
    op = _operator()
    op._chat_with_tools_with_fallback = MagicMock(side_effect=RuntimeError("all providers exhausted"))
    result = op.run_triage(_alert())
    # Never lose an alert — if triage breaks, investigate everything.
    assert result["action"] == "investigate"
    assert result["confidence"] == 0.0
    assert "unavailable" in result["reason"].lower()


def test_run_triage_falls_back_when_llm_returns_unparseable():
    op = _operator()
    op._chat_with_tools_with_fallback = MagicMock(return_value={
        "response": "I think this is a pod that is not ready.",  # prose, no JSON
        "tool_calls": 0,
    })
    result = op.run_triage(_alert())
    assert result["action"] == "investigate"
    assert "unparseable" in result["reason"]
    assert result["confidence"] == 0.0


def test_run_triage_falls_back_when_llm_picks_invalid_action():
    op = _operator()
    op._chat_with_tools_with_fallback = MagicMock(return_value={
        "response": '{"action": "ponder", "reason": "vibes", "confidence": 0.9}',
        "tool_calls": 0,
    })
    result = op.run_triage(_alert())
    # _parse_triage_response returns None for unknown actions; run_triage
    # treats that as unparseable.
    assert result["action"] == "investigate"


def test_run_triage_includes_similar_investigations_when_embeddings_available():
    op = _operator()
    op.embeddings.is_available.return_value = True
    op.embeddings.generate_embedding.return_value = [0.1] * 768
    op.kb._kb.find_similar_investigations_hybrid.return_value = [
        {"trigger": "Pod monitoring/promtail-foo not ready", "outcome": "resolved", "similarity": 0.87},
    ]
    captured = {}

    def fake_chat(messages, system_context, max_iterations=None, **kwargs):
        captured["messages"] = messages
        captured["system_context"] = system_context
        return {"response": '{"action": "notify", "reason": "similar precedent", "confidence": 0.9}',
                "tool_calls": 0}

    op._chat_with_tools_with_fallback = fake_chat
    op.run_triage(_alert())
    # The similar investigation should appear in the prompt so the LLM has
    # context for its decision.
    user_msg = captured["messages"][0]["content"]
    assert "Similar past investigations" in user_msg
    assert "promtail-foo" in user_msg


def test_run_triage_handles_alertmanager_payload_shape():
    """Real Alertmanager payloads use annotations.summary, not top-level summary."""
    op = _operator()
    captured = {}

    def fake_chat(messages, system_context, max_iterations=None, **kwargs):
        captured["messages"] = messages
        return {"response": '{"action": "investigate", "reason": "x", "confidence": 0.7}',
                "tool_calls": 0}

    op._chat_with_tools_with_fallback = fake_chat
    op.run_triage({
        "annotations": {"summary": "Pod X failing"},
        "labels": {"alertname": "PodNotReady"},
        "alert_id": "x",
    })
    # The legacy alertmanager-shape summary should still reach the prompt.
    assert "Pod X failing" in captured["messages"][0]["content"]
