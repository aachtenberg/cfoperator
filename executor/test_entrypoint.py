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


class _SeqLLM:
    """Returns canned replies in order (pass 1 = file path, pass 2 = diff)."""
    def __init__(self, replies):
        self.replies = list(replies); self.i = 0
    def complete(self, prompt):
        r = self.replies[self.i]; self.i += 1; return r


_FILES = ["k8s/base/apps/ollama.yaml", "k8s/base/apps/immich.yaml"]


def test_parse_selected_path():
    assert entrypoint.parse_selected_path("k8s/base/apps/ollama.yaml", _FILES) == _FILES[0]
    assert entrypoint.parse_selected_path("I'd edit k8s/base/apps/immich.yaml", _FILES) == _FILES[1]
    assert entrypoint.parse_selected_path("NONE", _FILES) is None
    assert entrypoint.parse_selected_path("nope/other.yaml", _FILES) is None


def test_run_two_pass_opens_pr():
    llm = _SeqLLM(["k8s/base/apps/ollama.yaml", _DIFF_REPORT])  # pass1 path, pass2 diff
    with patch.object(entrypoint, "make_llm", return_value=llm), \
         patch.object(entrypoint, "list_repo_files", return_value=["k8s/base/apps/ollama.yaml"]), \
         patch.object(entrypoint, "get_file", return_value="a\nb\nc\n"), \
         patch.object(entrypoint, "open_pr_from_diff",
                      return_value={"status": "opened", "html_url": "http://pr/1", "pr_number": 1}):
        payload = run(_env())
    assert payload["status"] == "pr-open" and payload["pr_url"] == "http://pr/1"


def test_run_needs_human_when_no_file_picked():
    with patch.object(entrypoint, "make_llm", return_value=_SeqLLM(["NONE"])), \
         patch.object(entrypoint, "list_repo_files", return_value=["a.yaml"]):
        payload = run(_env())
    assert payload["status"] == "needs-human" and "pick" in payload["detail"]


def test_run_needs_human_when_no_diff():
    llm = _SeqLLM(["k8s/base/apps/ollama.yaml", "can't do it safely; no diff"])
    with patch.object(entrypoint, "make_llm", return_value=llm), \
         patch.object(entrypoint, "list_repo_files", return_value=["k8s/base/apps/ollama.yaml"]), \
         patch.object(entrypoint, "get_file", return_value="x\n"):
        payload = run(_env())
    assert payload["status"] == "needs-human" and payload["pr_url"] is None
