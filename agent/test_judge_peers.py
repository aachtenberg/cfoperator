#!/usr/bin/env python3
"""Every mutation-judge peer tried is on the verdict (CFOP-318).

The judge's reason named the peer that decided, or the failures when none did.
A peer that was down before a later one confirmed was in the logs only
(CFOP-117 deferred this). The verdict now carries ``peers``, one entry per
provider tried, and every judge call writes one summary line.
"""

import json
import os
import sys
from unittest.mock import MagicMock

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator  # noqa: E402
import agent.agent as agent_mod  # noqa: E402


def _judge_op(complete, providers):
    op = MagicMock()
    op._parse_judge_verdict = CFOperator._parse_judge_verdict
    op._JUDGE_SYSTEM_PROMPT = CFOperator._JUDGE_SYSTEM_PROMPT
    op._judge_providers = lambda: list(providers)
    op._judge_model = lambda b: agent_mod._JUDGE_MODEL_FLOOR[b]
    op._judge_gitops_context = lambda details: ''
    op._complete_judge = complete
    return op


_DETAILS = {"trigger": "immich-kiosk crashloop", "provider": "ollama/gemma4:26b",
            "recommendation": "bump the memory limit", "repo": "aachtenberg/homelab-infra"}


def _http_error(status):
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(f"{status}: nope", response=resp)


def test_a_peer_that_was_down_before_one_confirmed_is_on_the_verdict(caplog):
    def complete(system, user, backend, model):
        if backend == "anthropic":
            raise requests.ConnectionError("connection refused")
        return '{"verdict": "confirm", "reason": "safe"}'

    op = _judge_op(complete, ("anthropic", "deepseek"))
    with caplog.at_level("INFO"):
        out = CFOperator._judge_mutation_remediation(op, dict(_DETAILS), "gitops-patch", "low", 1.0)
    assert out["verdict"] == "confirm" and out["backend"] == "deepseek"
    assert [(p["backend"], p["outcome"]) for p in out["peers"]] == [
        ("anthropic", "unavailable"), ("deepseek", "verdict")]
    assert "connection refused" in out["peers"][0]["detail"]
    assert all(isinstance(p["latency_ms"], int) for p in out["peers"])
    summary = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Mutation judge: ")]
    assert len(summary) == 1
    assert "verdict=confirm" in summary[0] and "anthropic:unavailable,deepseek:verdict" in summary[0]


def test_refused_self_review_and_unparseable_peers_are_recorded():
    def complete(system, user, backend, model):
        if backend == "anthropic":
            raise _http_error(400)
        return "I think it is probably fine"  # never the verdict format

    op = _judge_op(complete, ("deepseek", "anthropic", "gemini"))
    details = dict(_DETAILS, provider="deepseek/deepseek-v4-pro")
    out = CFOperator._judge_mutation_remediation(op, details, "gitops-patch", "low", 1.0)
    assert out["verdict"] == "downgrade"
    assert [(p["backend"], p["outcome"]) for p in out["peers"]] == [
        ("deepseek", "self_review_skipped"), ("anthropic", "refused"), ("gemini", "unparseable")]
    # The reply is kept out: it can quote the findings.
    assert "probably fine" not in json.dumps(out["peers"])


def test_every_return_path_carries_peers():
    op = _judge_op(MagicMock(side_effect=requests.ConnectionError("down")), ("anthropic",))
    out = CFOperator._judge_mutation_remediation(op, dict(_DETAILS), "gitops-patch", "low", 1.0)
    assert out["verdict"] == "downgrade" and [p["outcome"] for p in out["peers"]] == ["unavailable"]

    op = _judge_op(MagicMock(), ())
    out = CFOperator._judge_mutation_remediation(op, dict(_DETAILS), "gitops-patch", "low", 1.0)
    assert out["peers"] == []

    op = _judge_op(MagicMock(), ("deepseek",))
    out = CFOperator._judge_mutation_remediation(
        op, dict(_DETAILS, provider="deepseek/deepseek-v4-pro"), "gitops-patch", "low", 1.0)
    assert [p["outcome"] for p in out["peers"]] == ["self_review_skipped"]


def test_a_key_echoed_in_a_vendor_error_is_not_stored():
    """Keys go in headers, but a vendor error body could echo one; the
    stored detail is redacted like a tool result (claude-review on #318)."""
    def complete(system, user, backend, model):
        raise requests.ConnectionError("upstream said: invalid key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUV")

    op = _judge_op(complete, ("anthropic",))
    out = CFOperator._judge_mutation_remediation(op, dict(_DETAILS), "gitops-patch", "low", 1.0)
    assert "ABCDEFGHIJKLMNOP" not in json.dumps(out["peers"])
    assert "invalid key sk-" in out["peers"][0]["detail"]


def test_the_configured_key_is_not_stored_even_without_a_known_shape():
    """The redactor matches key shapes; a configured key may have none, so its
    literal value is removed as well (CodeRabbit on #318)."""
    def complete(system, user, backend, model):
        raise requests.ConnectionError("bad credential plainkey12345 for this endpoint")

    op = _judge_op(complete, ("anthropic",))
    op._judge_api_key = lambda backend: "plainkey12345"
    out = CFOperator._judge_mutation_remediation(op, dict(_DETAILS), "gitops-patch", "low", 1.0)
    assert "plainkey12345" not in json.dumps(out["peers"])
    assert "bad credential *** for this endpoint" in out["peers"][0]["detail"]
