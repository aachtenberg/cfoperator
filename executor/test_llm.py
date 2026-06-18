"""Tests for the swappable LLM backends and the env-driven factory."""

import io
import json
from unittest.mock import MagicMock, patch

import pytest

from llm import (
    AnthropicLLM,
    ClaudeCLILLM,
    LLMError,
    OpenAICompatLLM,
    make_llm,
)


def _fake_response(payload: dict):
    """A urlopen() context-manager stand-in returning JSON bytes."""
    cm = MagicMock()
    cm.__enter__.return_value = io.BytesIO(json.dumps(payload).encode("utf-8"))
    cm.__exit__.return_value = False
    return cm


# ---- factory selection -------------------------------------------------------


def test_make_llm_anthropic_default():
    llm = make_llm({"CFOP_EXEC_LLM_BACKEND": "anthropic", "ANTHROPIC_API_KEY": "sk"})
    assert isinstance(llm, AnthropicLLM)
    assert llm.model == "claude-opus-4-8"  # backend default


def test_make_llm_openai_requires_base_url():
    with pytest.raises(LLMError):
        make_llm({"CFOP_EXEC_LLM_BACKEND": "openai", "OPENAI_API_KEY": "sk"})


def test_make_llm_openai_covers_ollama_style():
    llm = make_llm({
        "CFOP_EXEC_LLM_BACKEND": "openai",
        "CFOP_EXEC_LLM_BASE_URL": "http://ubuntu-llm-01:11434/v1",
        "CFOP_EXEC_LLM_MODEL": "qwen2.5-coder",
    })
    assert isinstance(llm, OpenAICompatLLM)
    assert llm.base_url == "http://ubuntu-llm-01:11434/v1"
    assert llm.model == "qwen2.5-coder"


def test_make_llm_unknown_backend():
    with pytest.raises(LLMError):
        make_llm({"CFOP_EXEC_LLM_BACKEND": "wat"})


def test_make_llm_anthropic_needs_key():
    with pytest.raises(LLMError):
        make_llm({"CFOP_EXEC_LLM_BACKEND": "anthropic"})


# ---- backend request/parse ---------------------------------------------------


def test_openai_complete_parses_choices():
    llm = OpenAICompatLLM("http://x/v1", "m", "key")
    payload = {"choices": [{"message": {"content": "hello"}}]}
    with patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        assert llm.complete("hi") == "hello"


def test_anthropic_complete_concatenates_text_blocks():
    llm = AnthropicLLM("http://x", "m", "key")
    payload = {"content": [{"type": "text", "text": "foo"}, {"type": "text", "text": "bar"}]}
    with patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        assert llm.complete("hi") == "foobar"


def test_openai_complete_bad_shape_raises():
    llm = OpenAICompatLLM("http://x/v1", "m")
    with patch("urllib.request.urlopen", return_value=_fake_response({"nope": 1})):
        with pytest.raises(LLMError):
            llm.complete("hi")


def test_claude_cli_parses_result():
    llm = ClaudeCLILLM("claude-opus-4-8")
    proc = MagicMock(returncode=0, stdout=json.dumps({"result": "diff here"}), stderr="")
    with patch("subprocess.run", return_value=proc):
        assert llm.complete("hi") == "diff here"


def test_claude_cli_nonzero_raises():
    llm = ClaudeCLILLM("m")
    proc = MagicMock(returncode=1, stdout="", stderr="boom")
    with patch("subprocess.run", return_value=proc):
        with pytest.raises(LLMError):
            llm.complete("hi")
