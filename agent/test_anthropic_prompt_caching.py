#!/usr/bin/env python3
"""The Anthropic tool loop asks for prompt caching, and keeps its prompt a
stable prefix so the cache can hit.

Every iteration of the loop resends tools, system and the whole history. With
no cache_control anywhere, a week of Sonnet sweeps sent 23.5M input tokens at
full price, most of it the same prefix again (CFOP-461). A cache breakpoint
only pays if the bytes ahead of it are identical on the next request, so the
guard that matters is the prefix one: a later change that rewrites history or
puts something volatile ahead of the tail fails here instead of quietly
turning the cache off. These drive the real loop with requests.post patched.
"""

import copy
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from prometheus_client import REGISTRY

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator
from agent.agent import _prompt_cache_note

MODEL = "claude-sonnet-5-5"
SYSTEM = "You are CFOperator performing a proactive infrastructure sweep."

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "k8s_get_pods",
        "description": "List pods",
        "parameters": {"type": "object", "properties": {"namespace": {"type": "string"}}},
    },
}


def _response(body):
    resp = MagicMock()
    resp.status_code = 200
    resp.text = json.dumps(body)
    resp.json.return_value = body
    resp.raise_for_status.return_value = None
    return resp


def _usage(i=1, read=0, write=0):
    return {"input_tokens": i, "output_tokens": 1,
            "cache_read_input_tokens": read, "cache_creation_input_tokens": write}


def _tool_use(n, usage=None):
    # A thinking block rides along as Sonnet 5.5 returns one; it is passed
    # back unchanged and must not disturb the prefix either.
    return _response({
        "content": [{"type": "thinking", "thinking": "", "signature": f"sig{n}"},
                    {"type": "tool_use", "id": f"toolu_{n}", "name": "k8s_get_pods",
                     "input": {"namespace": f"ns{n}"}}],
        "stop_reason": "tool_use",
        "usage": usage or _usage()})


def _text(text, usage=None):
    return _response({"content": [{"type": "text", "text": text}],
                      "stop_reason": "end_turn", "usage": usage or _usage()})


class _Recorder:
    """requests.post stand-in that serializes each payload when it is sent.

    The loop reuses message dicts across iterations, so a recorded reference
    would show a later in-place edit as if it had been sent all along — which
    is exactly the edit the prefix test exists to catch.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []

    def __call__(self, url, json=None, **kwargs):
        if "api.anthropic.com/v1/messages" in str(url):
            self.payloads.append(copy.deepcopy(json))
        return self.responses.pop(0)


def _op(max_iterations=5):
    op = CFOperator.__new__(CFOperator)
    op.config = {"chat": {"max_tool_iterations": max_iterations}}
    op.llm_timeout = 5
    calls = iter(range(100))
    op.tools = SimpleNamespace(
        get_schemas=lambda: [TOOL_SCHEMA],
        # A different result every call, as live tools return.
        execute=lambda name, args: {"pods": [{"name": f"loki-{next(calls)}", "status": "Running"}]},
    )
    op.kb = SimpleNamespace(get_setting=lambda *a, **k: "")
    op.llm = SimpleNamespace(record_success=lambda *a: None,
                             record_failure=lambda *a: None,
                             classify_error=lambda *a, **k: "connection")
    op.provider_api_key = lambda provider: "k"
    return op


def _run(op, recorder, system=SYSTEM, wrapper=False):
    fn = CFOperator._chat_with_tools if wrapper else CFOperator._chat_with_tools_inner
    with patch("requests.post", recorder):
        return fn(op, "anthropic", "https://api.anthropic.com", MODEL,
                  [{"role": "user", "content": "check the cluster"}], system)


def _breakpoints(node):
    if isinstance(node, dict):
        return int("cache_control" in node) + sum(_breakpoints(v) for v in node.values())
    if isinstance(node, list):
        return sum(_breakpoints(v) for v in node)
    return 0


def _strip(node):
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if k != "cache_control"}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def _wire(node):
    # Insertion order, as requests serializes it: key order is part of the
    # bytes the cache keys on, so no sort_keys here.
    return json.dumps(_strip(node))


def test_every_tool_offering_request_marks_system_and_the_growing_tail():
    rec = _Recorder([_tool_use(1), _tool_use(2), _text("[]")])
    _run(_op(), rec)
    assert len(rec.payloads) == 3
    for body in rec.payloads:
        assert body.get("tools"), "these requests offer tools"
        assert body["system"] == [{"type": "text", "text": SYSTEM,
                                   "cache_control": {"type": "ephemeral"}}]
        assert body["cache_control"] == {"type": "ephemeral"}, \
            "the automatic breakpoint is what moves forward with the history"
        assert _breakpoints(body) <= 4, "Anthropic allows four breakpoints per request"


def test_each_request_is_a_byte_identical_prefix_of_the_next():
    rec = _Recorder([_tool_use(1), _tool_use(2), _tool_use(3), _text("[]")])
    _run(_op(), rec)
    assert len(rec.payloads) == 4
    for before, after in zip(rec.payloads, rec.payloads[1:]):
        # Render order is tools, system, messages; anything that differs
        # ahead of the previous request's last block misses the cache.
        assert after["model"] == before["model"]
        assert _wire(after["tools"]) == _wire(before["tools"])
        assert _wire(after["system"]) == _wire(before["system"])
        n = len(before["messages"])
        assert len(after["messages"]) > n
        assert [_wire(m) for m in after["messages"][:n]] == [_wire(m) for m in before["messages"]], \
            "an earlier message changed between iterations"


def test_a_request_without_tools_carries_no_breakpoint():
    # Budget of two: one tool turn, then the forced final turn, which
    # withholds tools. Tools render first, so that request shares no prefix
    # with the loop's entries and a marker would only add the write premium.
    rec = _Recorder([_tool_use(1), _text("[]")])
    _run(_op(max_iterations=2), rec)
    assert len(rec.payloads) == 2
    final = rec.payloads[1]
    assert "tools" not in final
    assert _breakpoints(final) == 0
    assert final["system"] == SYSTEM


def test_the_iteration_limit_summary_carries_no_breakpoint():
    rec = _Recorder([_tool_use(1), _text("summary")])
    result = _run(_op(max_iterations=1), rec)
    assert result["response"] == "summary"
    summary = rec.payloads[-1]
    assert "tools" not in summary
    assert _breakpoints(summary) == 0


def test_an_empty_system_context_is_not_sent_as_an_empty_block():
    # An empty text block is a 400 from Anthropic.
    rec = _Recorder([_text("[]")])
    _run(_op(), rec, system="")
    assert rec.payloads[0]["system"] == ""
    assert rec.payloads[0]["cache_control"] == {"type": "ephemeral"}


def _cache_sample(kind):
    return REGISTRY.get_sample_value(
        "cfoperator_llm_cache_tokens_total",
        {"provider": "anthropic", "model": MODEL, "kind": kind}) or 0


def test_cached_tokens_count_as_input_and_are_split_out():
    # Anthropic's input_tokens is only the uncached tail. Left alone, the
    # input series would fall about tenfold the day caching started and
    # read as the sweep doing less.
    rec = _Recorder([_tool_use(1, _usage(i=10, write=4000)),
                     _text("[]", _usage(i=20, read=4000, write=150))])
    read0, write0 = _cache_sample("read"), _cache_sample("write")
    result = _run(_op(), rec, wrapper=True)
    assert result["input_tokens"] == 10 + 4000 + 20 + 4000 + 150
    assert result["cache_read_tokens"] == 4000
    assert result["cache_write_tokens"] == 4150
    assert _cache_sample("read") - read0 == 4000
    assert _cache_sample("write") - write0 == 4150


def test_the_sweep_log_names_the_prompt_cache_only_when_there_is_one():
    assert _prompt_cache_note({"cache_read_tokens": 0, "cache_write_tokens": 0}) == ""
    assert _prompt_cache_note({}) == ""
    assert "4000 read" in _prompt_cache_note({"cache_read_tokens": 4000, "cache_write_tokens": 150})
