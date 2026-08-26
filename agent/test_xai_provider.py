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

class _StubKB:
    def get_setting(self, name, default=""):
        return default


def test_registry_has_the_openai_compatible_providers():
    # gemini joined groq and xai: it was named in config.yaml, config.yaml.example
    # and docs/config-reference.md as shipped, but existed in no code path, so a
    # gemini entry in the fallback chain was silently inert.
    assert set(OPENAI_COMPAT_PROVIDERS) == {"groq", "xai", "gemini", "deepseek"}
    for cfg in OPENAI_COMPAT_PROVIDERS.values():
        assert cfg["base_url"].startswith("https://")
        assert cfg["key_env"] and cfg["label"]


def test_deepseek_registry_points_at_deepseek_api():
    ds = OPENAI_COMPAT_PROVIDERS["deepseek"]
    assert ds["base_url"] == "https://api.deepseek.com/v1"
    assert ds["key_env"] == "DEEPSEEK_API_KEY"


def test_deepseek_resolves_to_v4_pro_with_no_config_and_no_console_choice():
    # "Default model is deepseek-v4-pro" has to be true with only a key set:
    # no llm.fallback entry, nothing chosen in Admin -> LLM. Before
    # default_model a key-only provider resolved to model='' and failed at
    # the vendor.
    op = CFOperator.__new__(CFOperator)
    op.kb = _StubKB()
    op.config = {"llm": {}}
    assert op._resolve_provider(backend="deepseek", model=None) == ("deepseek", None, "deepseek-v4-pro")


def test_registry_default_model_loses_to_config_and_console():
    class _KB:
        def get_setting(self, name, default=""):
            return "deepseek-v4-flash" if name == "deepseek_selected_model" else default
    op = CFOperator.__new__(CFOperator)
    op.kb = _StubKB()
    op.config = {"llm": {"fallback": [{"provider": "deepseek", "model": "from-config"}]}}
    assert op._resolve_provider(backend="deepseek", model=None)[2] == "from-config"
    op.kb = _KB()
    assert op._resolve_provider(backend="deepseek", model=None)[2] == "deepseek-v4-flash"


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

def test_resolve_provider_accepts_xai():
    op = CFOperator.__new__(CFOperator)
    op.kb = _StubKB()
    op.config = {"llm": {}}
    resolved = op._resolve_provider(backend="xai", model="grok-3")
    assert resolved == ("xai", None, "grok-3")


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
