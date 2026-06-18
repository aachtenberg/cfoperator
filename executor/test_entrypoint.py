"""Tests for the executor entrypoint orchestration + pure helpers."""

import json
import os
from unittest.mock import patch

import pytest

import entrypoint
from entrypoint import (
    build_completion_payload,
    build_prompt,
    classify_result,
    load_work_order,
    run,
)

_WORK_ORDER = {
    "id": 5,
    "investigation_id": 99,
    "remediation_class": "gitops-patch",
    "risk": "low",
    "confidence": 0.9,
    "payload": {
        "recommendation": "Restore CoreDNS forwarders",
        "rendered_context": "prior findings here",
        "repo": "aachtenberg/homelab-infra",
        "target": {"host": "raspberrypi5", "ns": "kube-system"},
    },
}

_DIFF_REPORT = """I will fix it.

```diff
--- a/k3s/base/apps/ollama.yaml
+++ b/k3s/base/apps/ollama.yaml
@@ -1,3 +1,3 @@
 a
-b
+B
 c
```
"""


# ---- pure helpers ------------------------------------------------------------


def test_load_work_order_valid_and_invalid():
    assert load_work_order({"CFOP_REMEDIATION_JSON": json.dumps(_WORK_ORDER)})["id"] == 5
    with pytest.raises(ValueError):
        load_work_order({"CFOP_REMEDIATION_JSON": ""})
    with pytest.raises(ValueError):
        load_work_order({"CFOP_REMEDIATION_JSON": json.dumps({"no": "id"})})


def test_build_prompt_substitutes():
    prompt = build_prompt(
        "Fix: {recommendation} | class {remediation_class} | repo {repo} | {context}",
        _WORK_ORDER,
    )
    assert "Restore CoreDNS forwarders" in prompt
    assert "gitops-patch" in prompt
    assert "aachtenberg/homelab-infra" in prompt
    assert "prior findings here" in prompt


def test_classify_result():
    assert classify_result(None, None)[0] == "needs-human"
    assert classify_result("d", {"status": "opened", "html_url": "u"}) == ("pr-open", "u", "opened")
    assert classify_result("d", {"status": "declined", "detail": "drift"})[0] == "needs-human"
    with pytest.raises(RuntimeError):
        classify_result("d", {"status": "error", "detail": "boom"})


def test_build_completion_payload_shape():
    p = build_completion_payload(_WORK_ORDER, "pr-open", "http://pr", "opened", {"x": 1})
    assert p == {"remediation_id": 5, "status": "pr-open", "pr_url": "http://pr",
                 "detail": "opened", "result": {"x": 1}}


# ---- run() orchestration -----------------------------------------------------


def _env(**extra):
    env = {
        "CFOP_REMEDIATION_JSON": json.dumps(_WORK_ORDER),
        "CFOP_TEMPLATES_DIR": os.path.join(os.path.dirname(__file__), "templates"),
        "CFOP_GIT_REPO": "aachtenberg/homelab-infra",
        "GITHUB_TOKEN": "ght",
    }
    env.update(extra)
    return env


def test_run_opens_pr_on_diff():
    class _LLM:
        def complete(self, prompt):
            return _DIFF_REPORT

    with patch.object(entrypoint, "make_llm", return_value=_LLM()), \
         patch.object(entrypoint, "open_pr_from_diff",
                      return_value={"status": "opened", "html_url": "http://pr/1", "pr_number": 1}):
        payload = run(_env())
    assert payload["status"] == "pr-open"
    assert payload["pr_url"] == "http://pr/1"
    assert payload["remediation_id"] == 5


def test_run_routes_to_human_when_no_diff():
    class _LLM:
        def complete(self, prompt):
            return "I can't safely mechanize this; needs a human."

    with patch.object(entrypoint, "make_llm", return_value=_LLM()):
        payload = run(_env())
    assert payload["status"] == "needs-human"
    assert payload["pr_url"] is None
