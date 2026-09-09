#!/usr/bin/env python3
"""The item a remediation row becomes in an issue tracker (CFOP-170).

Pure functions in tracker_item.py; no HTTP, no DB. What matters here is
what goes OUT to another party's system: the title derivation, the PR→high /
no-PR→low priority rule, and that raw tool output and credentials never do.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tracker_item import (  # noqa: E402
    BODY_MAX, TITLE_MAX, build_body, build_item, build_pr_comment, build_queued_comment,
    build_reparked_comment, build_transition_note, derive_title, labels_for, priority_for, scrub,
)


def _row(**over):
    row = {
        "id": 42, "status": "needs-human", "remediation_class": "gitops-patch", "risk": "low",
        "confidence": 0.7, "host_id": "raspberrypi2", "investigation_id": 2305, "attempts": 0,
        "pr_url": None, "named_pr_url": None, "last_error": None, "result": {},
        "payload": {"recommendation": "- Bump the memory limit for immich-server to 2Gi\n\nBecause OOM.",
                    "rendered_context": "SECRET-CONTEXT token=abc123 host 10.0.0.9",
                    "dedupe_key": "k8s:apps/immich-server:oom", "provider": "ollama/gemma4:26b",
                    "source": "investigation"},
    }
    row.update(over)
    return row


# --- title ---------------------------------------------------------------

def test_title_prefers_payload_title():
    assert derive_title(_row(payload={"title": "Disk full on pi2"})) == "[cfop #42] Disk full on pi2"


def test_title_falls_back_to_first_recommendation_line_stripped_of_markdown():
    assert derive_title(_row()) == "[cfop #42] Bump the memory limit for immich-server to 2Gi"
    assert derive_title(_row(payload={"recommendation": "## 1. Do X\nmore"})) == "[cfop #42] 1. Do X" \
        or derive_title(_row(payload={"recommendation": "## 1. Do X\nmore"})) == "[cfop #42] Do X"


def test_title_falls_back_to_class_on_host_and_is_capped():
    assert derive_title(_row(payload={})) == "[cfop #42] gitops-patch on raspberrypi2"
    long = derive_title(_row(payload={"title": "x" * 500}))
    assert len(long) <= TITLE_MAX and long.endswith("…")


# --- priority / labels ---------------------------------------------------

def test_priority_follows_the_pr_not_the_risk():
    assert priority_for(_row(risk="high")) == "low"
    assert priority_for(_row(risk="low", pr_url="https://github.com/o/r/pull/1")) == "high"
    assert priority_for(_row(pr_url="   ")) == "low"


def test_labels_name_the_class_and_the_reason_it_is_here():
    assert labels_for(_row()) == ["cfoperator", "needs-human", "gitops-patch", "risk:low"]
    assert labels_for(_row(pr_url="https://x/pull/1"))[1] == "pr-open"


# --- body ----------------------------------------------------------------

def test_body_carries_the_facts_and_the_console_link():
    body = build_body(_row(last_error="executor declined: multi-file diff"),
                      console_url="http://c/remediations#42")
    for needle in ("gitops-patch", "raspberrypi2", "0.7", "executor declined: multi-file diff",
                   "Bump the memory limit", "http://c/remediations#42", "Investigation: #2305",
                   "k8s:apps/immich-server:oom", "ollama/gemma4:26b", "Filed by cfoperator for remediation #42"):
        assert needle in body, needle


def test_body_omits_the_console_line_when_no_base_url_and_never_ships_rendered_context():
    body = build_body(_row(), console_url="")
    assert "Console:" not in body
    assert "SECRET-CONTEXT" not in body and "10.0.0.9" not in body
    item = build_item(_row(), console_base_url="")
    assert "SECRET-CONTEXT" not in item["body_markdown"]
    assert item["links"]["console_url"] is None


def test_body_reason_defaults_to_the_gate_when_no_error():
    assert "not auto-eligible" in build_body(_row())


def test_body_renders_steps_observed_and_pr_link():
    row = _row(pr_url="https://github.com/o/r/pull/9", status="pr-open",
               payload={"recommendation": "r", "steps": ["edit values.yaml", "merge"],
                        "observed": [{"source": "prometheus", "value": "mem 98%"}, "plain"]})
    body = build_body(row)
    assert "1. edit values.yaml" in body and "2. merge" in body
    assert "- prometheus → mem 98%" in body and "- plain" in body
    assert "PR: https://github.com/o/r/pull/9" in body and "waits for a human merge" in body


def test_body_is_capped():
    body = build_body(_row(payload={"recommendation": "x" * 10000, "steps": ["y" * 5000] * 5}))
    assert len(body) <= BODY_MAX and body.endswith("(truncated)")


# --- scrub ---------------------------------------------------------------

@pytest.mark.parametrize("text, gone", [
    ("token=abc123def", "abc123def"),
    ("api_key: sk-verysecretvalue1234567890", "sk-verysecretvalue1234567890"),
    ("Authorization: Bearer eyJhbGciOi.xx.yy", "eyJhbGciOi"),
    ("use ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123 here", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"),
    ("slack xoxb-1234567890-abcdefghij", "xoxb-1234567890"),
])
def test_scrub_redacts_credential_shapes(text, gone):
    out = scrub(text)
    assert gone not in out and "<redacted>" in out


def test_scrub_leaves_ordinary_text_alone():
    assert scrub("bump limit to 2Gi on raspberrypi2") == "bump limit to 2Gi on raspberrypi2"


# --- item ----------------------------------------------------------------

def test_build_item_is_the_contract_body():
    item = build_item(_row(), console_base_url="http://c/")
    assert item["remediation_id"] == 42 and item["investigation_id"] == 2305
    assert item["priority"] == "low" and item["title"].startswith("[cfop #42] ")
    assert item["links"] == {"console_url": "http://c/remediations#42", "pr_url": None}
    assert item["dedupe_key"] == "k8s:apps/immich-server:oom"
    assert item["host"] == "raspberrypi2" and item["remediation_class"] == "gitops-patch"
    assert build_item({"id": 1, "payload": None, "result": None})["title"] == "[cfop #1] remediation on unknown host"


# --- comments / notes ----------------------------------------------------

def test_pr_comment_and_reparked_and_queued_comments():
    assert "PR opened: https://x/pull/1" in build_pr_comment(_row(pr_url="https://x/pull/1"))
    assert "names https://x/pull/2" in build_pr_comment(_row(named_pr_url="https://x/pull/2"))
    assert "no reason recorded" in build_reparked_comment(_row())
    assert "multi-file" in build_reparked_comment(_row(last_error="executor declined: multi-file diff"))
    assert "attempt 3" in build_queued_comment(_row(attempts=2))


def test_transition_note_reads_who_and_why():
    assert build_transition_note(_row(result={"resolved_by": "operator", "resolution_note": "fixed by hand"}),
                                 "resolved") == "Resolved by operator. fixed by hand"
    assert build_transition_note(_row(result={"pr_merged": True}), "resolved") == "Resolved by PR merge."
    assert build_transition_note(_row(result={}), "resolved") == "Resolved by cfoperator."
    assert build_transition_note(_row(last_error="PR closed without merge"), "rejected") == \
        "Rejected. PR closed without merge"
    assert build_transition_note(_row(), "rejected") == "Rejected."
    assert "<redacted>" in build_transition_note(_row(result={"resolved_by": "operator",
                                                                "resolution_note": "token=abc"}), "resolved")
