"""Tests for the deep-investigation worker entrypoint (CI only, not shipped)."""

import io
import json
from unittest.mock import patch

import entrypoint
from entrypoint import (
    ClaudeRun,
    build_action_result,
    build_prompt,
    extract_proposed_diff,
    load_inputs,
    parse_recommendation,
    parse_status,
    post_completion,
)


def _inputs(**env_overrides):
    env = {
        "CFOP_ALERT_JSON": json.dumps(
            {
                "alert_id": "abc-123",
                "summary": "NodeUnreachable raspberrypi3",
                "severity": "critical",
                "occurred_at": "2026-06-11T08:00:00+00:00",
            }
        ),
        "CFOP_DEEP_CONTEXT_JSON": json.dumps(
            {"original_action": "escalate", "triage_reasoning": "node gone"}
        ),
        "CFOP_TEMPLATE": "host-forensics",
        "CFOP_TARGET_HOST": "raspberrypi3",
        "CFOP_SSH_USER": "aachten",
        "CLAUDE_MODEL": "claude-opus-4-8",
        "CFOP_CLAUDE_TIMEOUT": "600",
        "CFOP_COMPLETION_URL": "http://event-runtime:8000/v1/investigations/abc-123/complete",
        "CFOP_COMPLETION_TOKEN": "sekrit",
        "CFOP_AGENT_URL": "http://agent:8083",
    }
    env.update(env_overrides)
    return load_inputs(env)


_REPORT = """## Root cause
SD card went read-only after undervoltage event.

## Evidence
- `dmesg`: mmc0 I/O errors at 07:58
- `journalctl -b -1`: kernel watchdog reset

## Recommended fix
Move the node to NVMe boot.

```diff
--- a/k3s/base/daemonsets/node-exporter.yml
+++ b/k3s/base/daemonsets/node-exporter.yml
@@ -10,7 +10,7 @@
-  foo: bar
+  foo: baz
```

STATUS: needs_action
RECOMMENDATION: Replace the SD card and migrate raspberrypi3 to NVMe boot.
"""


# ---- input loading -----------------------------------------------------------


def test_load_inputs_parses_json_payloads():
    inputs = _inputs()
    assert inputs.alert["alert_id"] == "abc-123"
    assert inputs.deep_context["original_action"] == "escalate"
    assert inputs.target_host == "raspberrypi3"
    assert inputs.claude_timeout == 600


def test_load_inputs_tolerates_garbage_json():
    inputs = _inputs(CFOP_ALERT_JSON="{not json", CFOP_DEEP_CONTEXT_JSON="")
    assert inputs.alert == {}
    assert inputs.deep_context == {}


# ---- prompt rendering ---------------------------------------------------------


def test_build_prompt_substitutes_placeholders():
    template = "Host {host} via {ssh_user}: {alert_summary} ({severity} at {occurred_at})\n{prior_findings}"
    prompt = build_prompt(template, _inputs())
    assert "Host raspberrypi3 via aachten" in prompt
    assert "NodeUnreachable raspberrypi3" in prompt
    assert "critical" in prompt
    assert "triage_reasoning" in prompt  # prior findings serialized in


def test_build_prompt_survives_braces_in_template():
    # str.format would raise on stray braces; replacement must not.
    template = "{host} report with ```json\n{\"key\": 1}\n``` block"
    prompt = build_prompt(template, _inputs())
    assert prompt.startswith("raspberrypi3 report")
    assert '{"key": 1}' in prompt


def test_build_prompt_notes_missing_prior_findings():
    inputs = _inputs(CFOP_DEEP_CONTEXT_JSON="")
    prompt = build_prompt("{prior_findings}", inputs)
    assert "none" in prompt


# ---- report parsing -----------------------------------------------------------


def test_parse_status_extracts_trailing_status():
    assert parse_status(_REPORT) == "needs_action"
    assert parse_status("...\nSTATUS: resolved\nRECOMMENDATION: x") == "resolved"
    assert parse_status("...\nSTATUS: escalate\nRECOMMENDATION: x") == "escalate"


def test_parse_status_defaults_to_needs_action():
    assert parse_status("no status line here") == "needs_action"
    assert parse_status("STATUS: bogus_value") == "needs_action"


def test_parse_recommendation():
    rec = parse_recommendation(_REPORT)
    assert rec.startswith("Replace the SD card")
    assert parse_recommendation("nothing") == ""


def test_extract_proposed_diff_present():
    diff = extract_proposed_diff(_REPORT)
    assert diff is not None
    assert diff.startswith("--- a/k3s/base/daemonsets/node-exporter.yml")


def test_extract_proposed_diff_absent():
    assert extract_proposed_diff("no diff here") is None


def test_extract_proposed_diff_takes_first_of_multiple():
    report = "```diff\nfirst\n```\ntext\n```diff\nsecond\n```"
    assert extract_proposed_diff(report) == "first"


# ---- ActionResult construction -------------------------------------------------


def test_build_action_result_success_maps_outcome_and_hoists_fields():
    run = ClaudeRun(True, _REPORT, "", 42.0, cost_usd=0.31)
    result = build_action_result(run, _inputs())
    assert result["action"] == "deep_investigate"
    assert result["success"] is True
    assert result["quiet"] is False
    details = result["details"]
    assert details["outcome"] == "needs_action"
    assert details["recommendation"].startswith("Replace the SD card")
    assert details["proposed_diff"].startswith("---")
    assert details["host"] == "raspberrypi3"
    assert details["cost_usd"] == 0.31


def test_build_action_result_escalate_maps_to_escalated():
    # "escalated" is the exact outcome string the engine's escalation ledger
    # matches on.
    run = ClaudeRun(True, "report\nSTATUS: escalate\nRECOMMENDATION: look now", "", 5.0)
    result = build_action_result(run, _inputs())
    assert result["details"]["outcome"] == "escalated"


def test_build_action_result_failure_is_loud_failed():
    run = ClaudeRun(False, "", "claude timed out after 600s", 600.0)
    result = build_action_result(run, _inputs())
    assert result["success"] is False
    assert result["quiet"] is False
    assert result["details"]["outcome"] == "failed"
    assert "timed out" in result["message"]


def test_build_action_result_truncates_report():
    run = ClaudeRun(True, ("x" * 50_000) + "\nSTATUS: monitoring\nRECOMMENDATION: y", "", 5.0)
    result = build_action_result(run, _inputs())
    assert len(result["details"]["report"]) <= entrypoint._REPORT_MAX_CHARS


# ---- completion post-back -------------------------------------------------------


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_post_completion_sends_payload_and_token():
    inputs = _inputs()
    result = build_action_result(ClaudeRun(True, _REPORT, "", 1.0), inputs)
    captured = {}

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["token"] = request.get_header("X-cfop-token")
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(b"{}")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        assert post_completion(inputs, result) is True

    assert captured["url"].endswith("/v1/investigations/abc-123/complete")
    assert captured["token"] == "sekrit"
    assert captured["body"]["alert"]["alert_id"] == "abc-123"
    assert captured["body"]["result"]["action"] == "deep_investigate"


def test_post_completion_retries_then_fails(monkeypatch):
    inputs = _inputs()
    result = build_action_result(ClaudeRun(True, _REPORT, "", 1.0), inputs)
    calls = []

    def fake_urlopen(request, timeout=0):
        calls.append(request.full_url)
        raise OSError("connection refused")

    monkeypatch.setattr(entrypoint.time, "sleep", lambda _s: None)
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        assert post_completion(inputs, result) is False
    assert len(calls) == 3


def test_post_completion_without_url_fails_fast():
    inputs = _inputs(CFOP_COMPLETION_URL="")
    result = build_action_result(ClaudeRun(True, _REPORT, "", 1.0), inputs)
    assert post_completion(inputs, result) is False
