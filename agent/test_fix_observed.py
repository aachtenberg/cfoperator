#!/usr/bin/env python3
"""CFOP-89: a FIX's observed value against the gitops file it names.

The pure validator only checks shape. This is the read. `source` is not
executed. A disagreeing assignment refuses the FIX; a quote we cannot
check stays on the row as unverified.
"""

import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.agent import (  # noqa: E402
    CFOperator,
    _check_observed_against_targets,
)
from test_remediation_queue import _na_op  # noqa: E402
from test_structured_fix import _report, _valid_fix  # noqa: E402
import reverify as rv  # noqa: E402

_UNIT = """\
[Service]
MemoryHigh=16G
MemoryMax=20G
# the host has 128G; this cap is deliberate
"""

_LIVE = (Path(__file__).resolve().parents[1]
         / "charts" / "cfoperator" / "templates" / "configmap.yaml")


def _gitops(**overrides):
    fix = _valid_fix(**overrides)
    return fix


def _op(reader):
    op = _na_op()
    op._classify_needs_action_recommendation = MagicMock(return_value={
        "remediation_class": "manual", "risk": "high", "confidence": None,
        "host": None, "repo": None})
    op._read_gitops_target = reader
    return op


def _enqueue(op, fix):
    return op._queue_needs_action_remediation(
        89, "Promtail OOM",
        {"fingerprint": "fp-89", "labels": {"instance": "raspberrypi5"}},
        "raise memory limit to 512Mi", _report(fix),
        provider="ollama/gemma4:26b", structured_fix=fix)


def test_a_verbatim_quote_of_the_live_file_is_verified():
    """End to end against the chart file this repo actually ships.

    The reader returns those bytes — the same text get_file_contents
    would. The claimed line is a real assignment in that file.
    """
    text = _LIVE.read_text()
    assert "model: ${OLLAMA_MODEL}" in text
    repo_root = Path(__file__).resolve().parents[1]
    op = _op(lambda repo, path: text if path == str(_LIVE.relative_to(repo_root)) else None)
    fix = _gitops(
        targets=[{"kind": "gitops-manifest",
                  "id": str(_LIVE.relative_to(repo_root)),
                  "repo": "aachtenberg/cfoperator"}],
        observed=[{"source": "the configmap", "value": "model: ${OLLAMA_MODEL}"}],
    )
    rid = _enqueue(op, fix)
    assert rid == 9
    op._classify_needs_action_recommendation.assert_not_called()
    payload = op.kb.queue_remediation.call_args.kwargs["payload"]
    assert payload["observed_check"]["status"] == "verified"
    assert payload["observed_check"]["entries"][0]["result"] == "verified"
    assert fix["observed_check"]["status"] == "verified"


def test_whitespace_around_the_equals_still_verifies():
    op = _op(lambda repo, path: _UNIT)
    fix = _gitops(observed=[{"source": "override.conf", "value": "MemoryHigh = 16G"}])
    _enqueue(op, fix)
    op._classify_needs_action_recommendation.assert_not_called()
    check = op.kb.queue_remediation.call_args.kwargs["payload"]["observed_check"]
    assert check["status"] == "verified"


def test_a_disagreeing_assignment_is_refused_and_logged(caplog):
    """#78's shape: the file says 16G, the FIX says 8G. Mutation check:
    stop treating that as contradicted and the classifier is not called."""
    op = _op(lambda repo, path: _UNIT)
    fix = _gitops(observed=[{
        "source": "cat override.conf",
        "value": "MemoryHigh=8G",
    }])
    with caplog.at_level(logging.WARNING):
        _enqueue(op, fix)
    op._classify_needs_action_recommendation.assert_called_once()
    assert fix["observed_check"]["status"] == "contradicted"
    assert "MemoryHigh=8G" in fix["observed_check"]["reason"]
    assert "16G" in fix["observed_check"]["reason"]
    assert any("FIX rejected" in r.message and "contradicts" in r.message
               for r in caplog.records)
    # The fabricated FIX did not ride onto the row.
    payload = op.kb.queue_remediation.call_args.kwargs["payload"]
    assert "observed" not in payload
    assert "observed_check" not in payload


def test_eight_g_does_not_verify_inside_one_twenty_eight_g():
    op = _op(lambda repo, path: _UNIT)
    fix = _gitops(observed=[{"source": "the comment", "value": "8G"}])
    _enqueue(op, fix)
    op._classify_needs_action_recommendation.assert_not_called()
    check = op.kb.queue_remediation.call_args.kwargs["payload"]["observed_check"]
    assert check["status"] == "unverified"
    assert check["entries"][0]["result"] == "unverified"


def test_a_log_line_beside_a_real_quote_stays_unverified():
    op = _op(lambda repo, path: _UNIT)
    fix = _gitops(observed=[
        {"source": "override.conf", "value": "MemoryHigh=16G"},
        {"source": "journalctl -u ollama", "value": "connection reset by peer"},
    ])
    _enqueue(op, fix)
    op._classify_needs_action_recommendation.assert_not_called()
    check = op.kb.queue_remediation.call_args.kwargs["payload"]["observed_check"]
    assert check["status"] == "unverified"
    assert [e["result"] for e in check["entries"]] == ["verified", "unverified"]


def test_a_failed_read_does_not_refuse():
    def _boom(repo, path):
        raise OSError("github down")

    op = _op(_boom)
    fix = _gitops(observed=[{"source": "override.conf", "value": "MemoryHigh=8G"}])
    _enqueue(op, fix)
    op._classify_needs_action_recommendation.assert_not_called()
    check = op.kb.queue_remediation.call_args.kwargs["payload"]["observed_check"]
    assert check["status"] == "unverified"
    assert "could not read" in check["reason"]


def test_one_unread_target_blocks_a_contradiction():
    """Contradiction needs every gitops file. A 404 on the second target
    must not refuse a claim we could not fully check."""
    def _read(repo, path):
        if path.endswith("missing.conf"):
            return None
        return _UNIT

    op = _op(_read)
    fix = _gitops(targets=[
        {"kind": "gitops-manifest", "id": "apps/override.conf",
         "repo": "aachtenberg/homelab-infra"},
        {"kind": "gitops-manifest", "id": "apps/missing.conf",
         "repo": "aachtenberg/homelab-infra"},
    ], observed=[{"source": "override.conf", "value": "MemoryHigh=8G"}])
    _enqueue(op, fix)
    op._classify_needs_action_recommendation.assert_not_called()
    check = op.kb.queue_remediation.call_args.kwargs["payload"]["observed_check"]
    assert check["status"] == "unverified"
    assert "missing.conf" in check["reason"]


def test_a_host_fix_says_it_was_not_read():
    op = _op(lambda repo, path: _UNIT)
    fix = _gitops(
        targets=[{"kind": "host", "id": "raspberrypi5"}],
        risk="high",
        observed=[{"source": "systemctl is-active ssh", "value": "inactive"}],
    )
    _enqueue(op, fix)
    op._classify_needs_action_recommendation.assert_not_called()
    check = op.kb.queue_remediation.call_args.kwargs["payload"]["observed_check"]
    assert check["status"] == "unverified"
    assert check["reason"] == "no gitops-manifest target to read"


def test_the_github_reader_uses_file_contents_not_the_source_string():
    class Gh:
        def __init__(self):
            self.calls = []

        def get_file_contents(self, repo, path, ref=None):
            self.calls.append((repo, path, ref))
            return {"success": True, "content": _UNIT}

    gh = Gh()
    op = MagicMock()
    op.tools.github_tools = gh
    assert CFOperator._read_gitops_target(op, "aachtenberg/homelab-infra", "apps/override.conf") == _UNIT
    assert gh.calls == [("aachtenberg/homelab-infra", "apps/override.conf", None)]

    op.tools.github_tools = None
    assert CFOperator._read_gitops_target(op, "aachtenberg/homelab-infra", "apps/override.conf") is None

    class Down:
        def get_file_contents(self, repo, path, ref=None):
            return {"success": False, "error": "404"}

    op.tools.github_tools = Down()
    assert CFOperator._read_gitops_target(op, "r", "p") is None


def test_reverify_names_the_check_so_a_claim_is_not_evidence():
    row = {
        "id": 89, "created_at": "t", "remediation_class": "gitops-patch",
        "risk": "low", "host_id": "h", "payload": {
            "recommendation": "raise the cap",
            "observed": [{"source": "override.conf", "value": "MemoryHigh=16G"}],
            "observed_check": {"status": "unverified", "reason": "could not read apps/override.conf"},
        },
    }
    text = rv.build_question(row)
    assert "Observed check: unverified — could not read apps/override.conf" in text


def test_a_dotted_path_is_not_treated_as_the_files_key():
    """resources.limits.memory is not the key `memory`. Parsing the last
    segment would refuse a quote that merely mentions a common name."""
    result = _check_observed_against_targets(
        _gitops(observed=[{
            "source": "deploy yaml",
            "value": "resources.limits.memory: 8Gi",
        }]),
        lambda repo, path: "memory: 256Mi\n",
    )
    assert result["status"] == "unverified"
    assert result["entries"][0]["result"] == "unverified"


def test_two_settings_quoted_as_one_blob_verify_when_the_file_has_them_in_order():
    claim = "MemoryHigh=16G\nMemoryMax=20G"
    result = _check_observed_against_targets(
        _gitops(observed=[{"source": "override.conf", "value": claim}]),
        lambda repo, path: _UNIT,
    )
    assert result["status"] == "verified"
