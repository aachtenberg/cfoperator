#!/usr/bin/env python3
"""Every Anthropic Messages payload the agent builds carries no sampling
parameter, and a refused request says why.

Opus 4.7 and later answer `temperature`/`top_p`/`top_k` with a 400 whose body
reads "`temperature` is deprecated for this model." CFOP-117 fixed the judge
and left the other four sites alone; the console chat then hit the same 400
the day the chat seat moved to claude-opus-5-5 (CFOP-435). These drive the
real functions with requests.post patched, so a site that copies the old
payload shape fails here rather than in the console.
"""

import json
import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator

SAMPLING = {"temperature", "top_p", "top_k"}
MODEL = "claude-opus-5-5"

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "k8s_get_pods",
        "description": "List pods",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _response(status, body):
    resp = MagicMock()
    resp.status_code = status
    resp.text = json.dumps(body)
    resp.json.return_value = body
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(
            f"{status} Client Error: Bad Request for url: x", response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


def _text(text):
    return _response(200, {"content": [{"type": "text", "text": text}],
                           "stop_reason": "end_turn",
                           "usage": {"input_tokens": 1, "output_tokens": 1}})


def _tool_use():
    return _response(200, {
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "k8s_get_pods", "input": {}}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1, "output_tokens": 1}})


REFUSAL = {"type": "error", "error": {"type": "invalid_request_error",
                                      "message": "`temperature` is deprecated for this model."}}


def _chat_op(max_iterations=1):
    op = CFOperator.__new__(CFOperator)
    op.config = {"chat": {"max_tool_iterations": max_iterations}}
    op.llm_timeout = 5
    op.tools = SimpleNamespace(
        get_schemas=lambda: [TOOL_SCHEMA],
        execute=lambda name, args: {"pods": [{"name": "loki-0", "status": "Running"}]},
    )
    op.kb = SimpleNamespace(get_setting=lambda *a, **k: "")
    op.llm = SimpleNamespace(record_success=lambda *a: None,
                             record_failure=lambda *a: None,
                             classify_error=lambda *a, **k: "connection")
    op.provider_api_key = lambda provider: "k"
    return op


def _anthropic_payloads(post):
    out = []
    for call in post.call_args_list:
        url = call.args[0] if call.args else call.kwargs.get("url")
        if "api.anthropic.com/v1/messages" in str(url):
            out.append(call.kwargs["json"])
    return out


def _run_chat(op, post):
    with patch("requests.post", post):
        return CFOperator._chat_with_tools_inner(
            op, "anthropic", "https://api.anthropic.com", MODEL,
            [{"role": "user", "content": "what is wrong with loki?"}], "You are an SRE.")


def test_chat_tool_loop_sends_no_sampling_parameter():
    post = MagicMock(return_value=_text("loki is fine"))
    result = _run_chat(_chat_op(), post)
    assert result["response"] == "loki is fine"
    payloads = _anthropic_payloads(post)
    assert payloads, "the chat loop never reached Anthropic"
    for body in payloads:
        assert body["model"] == MODEL
        assert not SAMPLING & set(body), sorted(body)


def test_chat_summary_call_sends_no_sampling_parameter():
    # Budget of one: the model calls a tool on its only turn, the loop runs
    # out, and the no-tools summary call is the second request.
    post = MagicMock(side_effect=[_tool_use(), _text("summary: loki is fine")])
    result = _run_chat(_chat_op(max_iterations=1), post)
    assert result["response"] == "summary: loki is fine"
    payloads = _anthropic_payloads(post)
    assert len(payloads) == 2, "expected the tool turn and the summary call"
    assert "tools" not in payloads[1], "the summary call offers no tools"
    for body in payloads:
        assert not SAMPLING & set(body), sorted(body)


def test_chat_refusal_names_the_vendors_message_not_just_the_status():
    # raise_for_status carries the status line only; the body is what tells
    # an operator what to fix, and this exception text is what the fallback
    # warning and the console show.
    post = MagicMock(return_value=_response(400, REFUSAL))
    with pytest.raises(requests.HTTPError) as exc:
        _run_chat(_chat_op(), post)
    assert "400" in str(exc.value)
    assert "`temperature` is deprecated" in str(exc.value)


def test_chat_summary_refusal_names_the_vendors_message_too(caplog):
    # The summary step answers with a generic line and logs the failure; the
    # log is where an operator reads what went wrong, so the vendor's words
    # must reach it rather than the bare status line.
    post = MagicMock(side_effect=[_tool_use(), _response(400, REFUSAL)])
    with caplog.at_level(logging.WARNING):
        result = _run_chat(_chat_op(max_iterations=1), post)
    assert "Maximum tool iterations reached" in result["response"]
    assert "`temperature` is deprecated" in caplog.text


class _KB:
    def __init__(self):
        self.stored = []

    def get_setting(self, key, default=""):
        return default

    def get_operational_summary(self, hours=24):
        return {"sweeps": {}, "investigations": {"total": 1}, "learnings": {}}

    def find_learnings(self, **kwargs):
        return []

    def store_learning(self, insight):
        self.stored.append(dict(insight))
        return 1

    _kb = SimpleNamespace(find_correlated_events=lambda **k: [],
                          get_service_correlations=lambda **k: [])


def _one_shot_op():
    op = CFOperator.__new__(CFOperator)
    op.config = {"llm": {"primary": {"url": "http://ollama:11434", "model": "gemma4:26b"}},
                 "notifications": {}}
    op.llm_timeout = 5
    op.notifications = []
    op.tools = SimpleNamespace(k8s_tools=SimpleNamespace(
        get_namespaces=lambda: {"success": True, "namespaces": [{"name": "apps"}]}))
    op.kb = _KB()
    op._embed_learning = MagicMock()
    op._resolve_provider = lambda: ("anthropic", "https://api.anthropic.com", MODEL)
    op.provider_api_key = lambda provider: "k"
    return op


def test_correlations_send_no_sampling_parameter():
    op = _one_shot_op()
    post = MagicMock(return_value=_text(json.dumps({"insights": []})))
    with patch("requests.post", post):
        op._analyze_correlations([{"severity": "warning", "finding": "load failed"}], [])
    payloads = _anthropic_payloads(post)
    assert len(payloads) == 1
    assert payloads[0]["model"] == MODEL
    assert not SAMPLING & set(payloads[0]), sorted(payloads[0])


def test_learning_extraction_sends_no_sampling_parameter():
    op = _one_shot_op()
    post = MagicMock(return_value=_text(json.dumps({"learnings": []})))
    with patch("requests.post", post):
        op._extract_learnings(7, "loki restart", {"status": "resolved"})
    payloads = _anthropic_payloads(post)
    assert len(payloads) == 1
    assert payloads[0]["model"] == MODEL
    assert not SAMPLING & set(payloads[0]), sorted(payloads[0])
