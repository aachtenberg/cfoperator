#!/usr/bin/env python3
"""Tests for the xAI Grok provider and the OpenAI-compatible provider registry.

xAI Grok speaks the same OpenAI-compatible dialect as Groq, so both are served
by one code path keyed on OPENAI_COMPAT_PROVIDERS. These tests pin the registry
shape, endpoint/key resolution, and that provider resolution accepts 'xai'.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator, OPENAI_COMPAT_PROVIDERS


# --- registry ---------------------------------------------------------------

def test_registry_has_groq_and_xai():
    assert set(OPENAI_COMPAT_PROVIDERS) == {"groq", "xai"}
    for cfg in OPENAI_COMPAT_PROVIDERS.values():
        assert cfg["base_url"].startswith("https://")
        assert cfg["key_env"] and cfg["label"]


def test_xai_registry_points_at_xai_api():
    xai = OPENAI_COMPAT_PROVIDERS["xai"]
    assert xai["base_url"] == "https://api.x.ai/v1"
    assert xai["key_env"] == "XAI_API_KEY"


# --- _openai_compat_request_config ------------------------------------------

def test_request_config_builds_chat_completions_url():
    os.environ["XAI_API_KEY"] = "test-xai-key"
    try:
        key, url = CFOperator._openai_compat_request_config("xai")
        assert key == "test-xai-key"
        assert url == "https://api.x.ai/v1/chat/completions"
    finally:
        del os.environ["XAI_API_KEY"]


def test_request_config_groq_url():
    key, url = CFOperator._openai_compat_request_config("groq")
    assert url == "https://api.groq.com/openai/v1/chat/completions"


def test_request_config_missing_key_returns_empty_string():
    os.environ.pop("XAI_API_KEY", None)
    key, url = CFOperator._openai_compat_request_config("xai")
    assert key == ""                                  # caller raises on empty
    assert url == "https://api.x.ai/v1/chat/completions"


def test_request_config_unknown_provider():
    key, url = CFOperator._openai_compat_request_config("ollama")
    assert key is None and url is None


# --- _resolve_provider accepts xai ------------------------------------------

class _StubKB:
    def get_setting(self, name, default=""):
        return default


def test_resolve_provider_accepts_xai():
    op = CFOperator.__new__(CFOperator)
    op.kb = _StubKB()
    op.config = {"llm": {}}
    resolved = op._resolve_provider(backend="xai", model="grok-3")
    assert resolved == ("xai", None, "grok-3")


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
