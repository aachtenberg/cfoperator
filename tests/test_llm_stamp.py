"""The investigations list carries the model that wrote the report.

The page renders ``provider`` and nothing else. The list endpoint used to
drop findings, so the column had nothing to read. A missing model stays
absent — the console shows an em dash rather than "unknown".
"""

from repo_paths import REPO_ROOT
import sys

ROOT = REPO_ROOT
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT))

from agent.knowledge_base import _investigation_provider  # noqa: E402


def test_investigation_provider_is_the_served_model():
    assert _investigation_provider({"provider": "ollama/qwen3-coder:latest"}) == (
        "ollama/qwen3-coder:latest"
    )
    assert _investigation_provider({"provider": "  groq/openai/gpt-oss-120b  "}) == (
        "groq/openai/gpt-oss-120b"
    )


def test_investigation_provider_is_absent_when_no_model_ran():
    assert _investigation_provider({}) is None
    assert _investigation_provider(None) is None
    assert _investigation_provider({"provider": ""}) is None
    assert _investigation_provider({"provider": "   "}) is None
    assert _investigation_provider({"provider": 3}) is None


def test_the_list_payload_carries_the_provider():
    src = (ROOT / "agent" / "knowledge_base.py").read_text(encoding="utf-8")
    start = src.index("def get_recent_investigations")
    body = src[start:start + 2500]
    assert '"provider": _investigation_provider(inv.findings)' in body
