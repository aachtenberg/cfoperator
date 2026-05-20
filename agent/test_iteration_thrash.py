#!/usr/bin/env python3
"""Tests for the sweep iteration-thrash mitigations.

Sweep phases were looping up to 50 iterations, re-ingesting untrimmed tool
output every turn (observed: 460 tool calls / 1.45M input tokens in one phase).
These cover the three mitigations: a small sweep-specific iteration cap, a
per-tool-result size cap, and forcing a text answer on the final iteration.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator


def _operator(config=None):
    op = CFOperator.__new__(CFOperator)
    op.config = config or {}
    return op


# --- _serialize_tool_result -------------------------------------------------

def test_small_result_passes_through_unchanged():
    op = _operator()
    result = {"status": "ok", "pods": 3}
    assert op._serialize_tool_result(result, 6000) == json.dumps(result, default=str)


def test_oversized_result_is_truncated_with_marker():
    op = _operator()
    big = {"logs": "x" * 50000}
    out = op._serialize_tool_result(big, 6000)
    assert len(out) < 6200  # head + short marker
    assert "truncated" in out
    assert out.startswith('{"logs": "xxx')


def test_truncation_marker_reports_omitted_size():
    op = _operator()
    out = op._serialize_tool_result({"v": "y" * 20000}, 1000)
    assert out.startswith('{"v": "yyy')
    # full payload is ~20020 chars, so ~19000 omitted
    assert "truncated 1" in out and "chars of tool output" in out


def test_non_json_native_result_does_not_crash():
    op = _operator()
    out = op._serialize_tool_result({"when": object()}, 6000)
    assert isinstance(out, str) and "when" in out


# --- _get_sweep_max_iterations ---------------------------------------------

def test_sweep_iterations_default_is_small():
    assert _operator({})._get_sweep_max_iterations() == 12


def test_sweep_iterations_honor_config_override():
    op = _operator({"ooda": {"sweep": {"max_iterations": 6}}})
    assert op._get_sweep_max_iterations() == 6


def test_sweep_iterations_are_clamped():
    # absurd values are clamped into [2, 20]
    assert _operator({"ooda": {"sweep": {"max_iterations": 500}}})._get_sweep_max_iterations() == 20
    assert _operator({"ooda": {"sweep": {"max_iterations": 1}}})._get_sweep_max_iterations() == 2


def test_sweep_iterations_independent_of_chat_max():
    # The global chat max_tool_iterations (used for interactive chat) must not
    # leak into sweeps — that coupling is what allowed 50-iteration phases.
    op = _operator({"chat": {"max_tool_iterations": 50}})
    assert op._get_sweep_max_iterations() == 12


# --- _max_tool_result_chars -------------------------------------------------

def test_tool_result_cap_default():
    assert _operator({})._max_tool_result_chars() == 6000


def test_tool_result_cap_override_and_floor():
    assert _operator({"chat": {"max_tool_result_chars": 3000}})._max_tool_result_chars() == 3000
    assert _operator({"chat": {"max_tool_result_chars": 10}})._max_tool_result_chars() == 500


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
