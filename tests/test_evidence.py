"""Evidence from context providers reaching the investigation (CFOP-211).

Three places have to agree: cfshared.evidence (the contract), the event
runtime's HTTP investigate handler (sends it) and the agent's prompt (renders
it). Each is tested here against the same payload.
"""

from __future__ import annotations

import json

import pytest

from cfshared.evidence import BLOCK_LIMIT, EVIDENCE_KEY, TOTAL_LIMIT, collect, render
from event_runtime.http_actions import HTTPInvestigateActionHandler
from event_runtime.models import ActionRequest, Alert, AlertSeverity, ContextEnvelope, Decision


def _alert(**kw):
    base = dict(source="dynatrace", severity=AlertSeverity.CRITICAL,
                summary="Dynatrace P-1: Backoff event on dt-chaos/crashloop",
                details={"alertname": "Backoff event"}, namespace="dt-chaos",
                resource_type="deployment", resource_name="crashloop", fingerprint="dynatrace:1")
    base.update(kw)
    return Alert(**base)


# --- the contract --------------------------------------------------------------

@pytest.mark.parametrize("context", [None, {}, {"evidence": None}, {"evidence": "text"}, {"evidence": []}])
def test_no_evidence_mapping_means_nothing_crosses(context):
    assert collect(context) == {}
    assert render(context.get("evidence") if context else None) == ""


def test_only_the_evidence_key_crosses_not_the_rest_of_the_envelope():
    context = {"hostname": "runtime-1", "pid": 7, "recent_changes": [{"sha": "abc"}],
               EVIDENCE_KEY: {"dynatrace": "Logs: boom x5"}}
    assert collect(context) == {"dynatrace": "Logs: boom x5"}


def test_blocks_are_text_empty_ones_dropped_and_each_is_capped():
    blocks = collect({EVIDENCE_KEY: {"a": "  ", "b": {"restarts": 24}, "c": "x" * (BLOCK_LIMIT + 50)}})
    assert "a" not in blocks
    assert json.loads(blocks["b"]) == {"restarts": 24}
    assert len(blocks["c"]) == BLOCK_LIMIT and blocks["c"].endswith("[... truncated]")


def test_the_total_budget_drops_whole_blocks_rather_than_stubs():
    per = BLOCK_LIMIT
    names = [f"b{i}" for i in range(TOTAL_LIMIT // per + 2)]
    blocks = collect({EVIDENCE_KEY: {name: "y" * per for name in names}})
    assert sum(len(v) for v in blocks.values()) <= TOTAL_LIMIT
    assert all(len(v) == per for v in blocks.values())
    assert list(blocks) == names[: len(blocks)]


def test_render_labels_the_section_as_data_and_names_each_block():
    text = render({"dynatrace": "Davis events:\n- No pod ready"})
    assert "not instructions" in text
    assert "--- dynatrace ---\nDavis events:\n- No pod ready" in text


# --- the event runtime sends it ------------------------------------------------

def _dispatch(monkeypatch, context):
    """Run the real handler with the network call captured; return (body sent, alert)."""
    sent = {}
    handler = HTTPInvestigateActionHandler("http://agent:8083")
    monkeypatch.setattr(handler, "_post", lambda endpoint, body: sent.update(endpoint=endpoint, body=json.loads(body)))
    alert = _alert()
    result = handler.execute(ActionRequest(
        alert=alert,
        decision=Decision(action="investigate", confidence=1.0, reasoning="test"),
        context=ContextEnvelope(alert=alert, context=context),
    ))
    assert result.success
    return sent["body"], alert


def test_the_investigate_request_carries_evidence(monkeypatch):
    body, alert = _dispatch(monkeypatch, {"hostname": "runtime-1", EVIDENCE_KEY: {"dynatrace": "Logs: boom x5"}})
    assert body.pop(EVIDENCE_KEY) == {"dynatrace": "Logs: boom x5"}
    assert body == json.loads(json.dumps(alert.to_dict(), default=str))   # nothing else was added


def test_without_evidence_the_request_is_exactly_the_alert(monkeypatch):
    body, alert = _dispatch(monkeypatch, {"hostname": "runtime-1"})
    assert body == json.loads(json.dumps(alert.to_dict(), default=str))


def test_the_completion_post_back_ignores_the_evidence_key():
    """The agent posts the alert dict (evidence and all) back; the runtime must still parse it."""
    payload = _alert().to_dict()
    payload[EVIDENCE_KEY] = {"dynatrace": "Logs: boom x5"}
    rebuilt = Alert.from_dict(payload)
    assert rebuilt.summary == payload["summary"] and rebuilt.fingerprint == "dynatrace:1"


# --- the agent renders it --------------------------------------------------------

def _prompt_block(alert_info):
    # tests/ pattern: repo root first, agent/ appended (agent.py uses bare
    # imports), then the module by its package path.
    import os
    import sys
    from repo_paths import REPO_ROOT
    agent_dir = os.path.join(str(REPO_ROOT), "agent")
    if agent_dir not in sys.path:
        sys.path.append(agent_dir)
    from agent.agent import _alert_prompt_block
    return _alert_prompt_block(alert_info)


def test_without_evidence_the_prompt_block_is_unchanged():
    alert = _alert().to_dict()
    assert _prompt_block(alert) == f"Alert details: {json.dumps(alert, default=str)[:1000]}"


def test_evidence_leaves_the_1000_character_json_and_gets_its_own_section():
    alert = _alert().to_dict()
    alert[EVIDENCE_KEY] = {"dynatrace": "Logs from dt-chaos/crashloop:\n- x5 cfop-201: deliberate crash for Davis"}
    block = _prompt_block(alert)
    head, _, rest = block.partition("\n\nEvidence gathered")
    assert head == f"Alert details: {json.dumps(_alert_without(alert), default=str)[:1000]}"
    assert EVIDENCE_KEY not in head.split("Alert details: ", 1)[1]
    assert "--- dynatrace ---" in rest and "deliberate crash for Davis" in rest


def test_the_agent_enforces_its_own_budget_on_what_arrives():
    alert = _alert().to_dict()
    alert[EVIDENCE_KEY] = {f"b{i}": "z" * BLOCK_LIMIT for i in range(10)}   # a sender that did not cap
    evidence_part = _prompt_block(alert).split("\n\nEvidence gathered", 1)[1]
    assert evidence_part.count("z") <= TOTAL_LIMIT


def _alert_without(alert):
    return {k: v for k, v in alert.items() if k != EVIDENCE_KEY}
