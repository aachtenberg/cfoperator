"""Tests for the discovery LLM backends and the env-driven factory."""

import io
import json
from unittest.mock import MagicMock, patch

import pytest

from llm import AnthropicLLM, LLMError, OpenAICompatLLM, make_llm


def _fake_response(payload: dict):
    """A urlopen() context-manager stand-in returning JSON bytes."""
    cm = MagicMock()
    cm.__enter__.return_value = io.BytesIO(json.dumps(payload).encode("utf-8"))
    cm.__exit__.return_value = False
    return cm


# ---- factory selection -------------------------------------------------------


def test_default_backend_is_openai_and_requires_base_url():
    """Local-model-by-default is a stated discovery bound: the default backend
    is the OpenAI-compat shape (Ollama), and the error says how to point it."""
    with pytest.raises(LLMError, match="CFOP_DISCOVERY_LLM_BASE_URL"):
        make_llm({})


def test_make_llm_openai_covers_ollama_style():
    llm = make_llm({
        "CFOP_DISCOVERY_LLM_BASE_URL": "http://ubuntu-llm-01:11434/v1",
        "CFOP_DISCOVERY_LLM_MODEL": "gemma4:26b",
    })
    assert isinstance(llm, OpenAICompatLLM)
    assert llm.base_url == "http://ubuntu-llm-01:11434/v1"
    assert llm.model == "gemma4:26b"


def test_make_llm_anthropic():
    llm = make_llm({"CFOP_DISCOVERY_LLM_BACKEND": "anthropic", "ANTHROPIC_API_KEY": "sk"})
    assert isinstance(llm, AnthropicLLM)


def test_make_llm_anthropic_requires_key():
    with pytest.raises(LLMError):
        make_llm({"CFOP_DISCOVERY_LLM_BACKEND": "anthropic"})


def test_make_llm_unknown_backend():
    with pytest.raises(LLMError):
        make_llm({"CFOP_DISCOVERY_LLM_BACKEND": "carrier-pigeon"})


def test_discovery_env_names_not_executor_names():
    """The component is standalone: it must read CFOP_DISCOVERY_LLM_*, not the
    executor's CFOP_EXEC_LLM_* (a copy-paste that would silently cross-wire
    two components' model configs)."""
    with pytest.raises(LLMError):
        make_llm({"CFOP_EXEC_LLM_BACKEND": "anthropic", "ANTHROPIC_API_KEY": "sk",
                  "CFOP_EXEC_LLM_BASE_URL": "http://x/v1"})


# ---- response parsing --------------------------------------------------------


def test_openai_compat_parses_content():
    llm = OpenAICompatLLM("http://x/v1", "m")
    with patch("llm.urllib.request.urlopen",
               return_value=_fake_response({"choices": [{"message": {"content": "hi"}}]})):
        assert llm.complete("p") == "hi"


def test_openai_compat_bad_shape_raises():
    llm = OpenAICompatLLM("http://x/v1", "m")
    with patch("llm.urllib.request.urlopen", return_value=_fake_response({"nope": 1})):
        with pytest.raises(LLMError):
            llm.complete("p")


def test_transport_failures_become_llm_error():
    """main() only catches LLMError/ValueError — an unreachable backend or a
    non-2xx must exit 1 with a message, not abort the Job with a traceback."""
    import io as _io
    import urllib.error

    llm = OpenAICompatLLM("http://x/v1", "m")
    http_err = urllib.error.HTTPError("http://x", 500, "boom", None, _io.BytesIO(b"overloaded"))
    with patch("llm.urllib.request.urlopen", side_effect=http_err):
        with pytest.raises(LLMError, match="HTTP 500.*overloaded"):
            llm.complete("p")
    with patch("llm.urllib.request.urlopen",
               side_effect=urllib.error.URLError("connection refused")):
        with pytest.raises(LLMError, match="request failed"):
            llm.complete("p")
    with patch("llm.urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        with pytest.raises(LLMError):
            llm.complete("p")


def test_anthropic_concatenates_text_blocks():
    llm = AnthropicLLM("http://x", "m", "sk")
    payload = {"content": [{"type": "text", "text": "a"},
                           {"type": "tool_use"},
                           {"type": "text", "text": "b"}]}
    with patch("llm.urllib.request.urlopen", return_value=_fake_response(payload)):
        assert llm.complete("p") == "ab"
