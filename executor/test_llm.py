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


# --- chain: ordered rungs, first that answers wins ---------------------------

import io
import urllib.error

from llm import BackendHTTPError, ChainLLM, parse_chain  # noqa: E402


def _http_error(status, body):
    return urllib.error.HTTPError("http://x", status, "Bad Request", {}, io.BytesIO(body.encode()))


_CHAIN = [
    {"backend": "anthropic", "model": "claude-opus-4-8", "api_key_env": "ANTHROPIC_API_KEY"},
    {"backend": "openai", "model": "anthropic/claude-opus-4.8",
     "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"},
]


def _chain_env(**extra):
    env = {"CFOP_EXEC_LLM_CHAIN": json.dumps(_CHAIN), "ANTHROPIC_API_KEY": "a", "OPENROUTER_API_KEY": "o"}
    env.update(extra)
    return env


def test_chain_env_builds_rungs_in_order():
    llm = make_llm(_chain_env())
    assert isinstance(llm, ChainLLM)
    assert [type(x).__name__ for x in llm.llms] == ["AnthropicLLM", "OpenAICompatLLM"]
    assert llm.llms[1].api_key == "o"  # each rung reads the env var it names


def test_chain_falls_through_on_http_error_and_keeps_the_body():
    """A 400 from the primary (the exhausted-credit case) moves to the next rung,
    and the attempt record says why in the provider's words, not urllib's."""
    llm = make_llm(_chain_env())
    ok = _fake_response({"choices": [{"message": {"content": "diff here"}}]})
    with patch("urllib.request.urlopen",
               side_effect=[_http_error(400, '{"error":{"message":"Your credit balance is too low"}}'), ok]):
        assert llm.complete("p") == "diff here"
    d = llm.describe()
    assert d["rung"] == 1 and d["backend"] == "openai" and d["model"] == "anthropic/claude-opus-4.8"
    assert len(d["attempts"]) == 1
    assert d["attempts"][0]["rung"] == 0 and "credit balance is too low" in d["attempts"][0]["error"]
    assert "HTTP 400" in d["attempts"][0]["error"]


def test_chain_all_rungs_failed_names_each_reason():
    llm = make_llm(_chain_env())
    with patch("urllib.request.urlopen",
               side_effect=[_http_error(400, "no credit"), urllib.error.URLError("refused")]):
        with pytest.raises(LLMError) as ei:
            llm.complete("p")
    msg = str(ei.value)
    assert "all 2 rung(s) failed" in msg and "anthropic/claude-opus-4-8: HTTP 400: no credit" in msg
    assert "openai/anthropic/claude-opus-4.8" in msg and "refused" in msg


def test_chain_skips_a_rung_it_cannot_build():
    """No key for the primary: it is recorded as unavailable and the next rung answers."""
    llm = make_llm(_chain_env(ANTHROPIC_API_KEY=""))
    assert llm.llms[0] is None and "ANTHROPIC_API_KEY" in llm.build_errors[0]
    with patch("urllib.request.urlopen",
               return_value=_fake_response({"choices": [{"message": {"content": "x"}}]})):
        assert llm.complete("p") == "x"
    assert llm.describe()["attempts"][0]["error"].startswith("unavailable:")


def test_chain_with_no_buildable_rung_fails_at_build():
    with pytest.raises(LLMError, match="no usable rung"):
        make_llm({"CFOP_EXEC_LLM_CHAIN": json.dumps([{"backend": "anthropic"}])})


def test_chain_rejects_bad_json_and_shapes():
    with pytest.raises(LLMError, match="not valid JSON"):
        parse_chain("{nope")
    with pytest.raises(LLMError, match="non-empty"):
        parse_chain("[]")
    with pytest.raises(LLMError, match="rung 0 is not an object"):
        parse_chain("[1]")


def test_openai_extra_body_is_merged_into_the_request():
    """OpenRouter's provider-routing object rides along; model/messages still win."""
    rung = [{"backend": "openai", "model": "m", "base_url": "http://x/v1", "api_key_env": "K",
             "extra_body": {"provider": {"sort": "price"}, "model": "ignored"}}]
    llm = make_llm({"CFOP_EXEC_LLM_CHAIN": json.dumps(rung), "K": "k"})
    seen = {}

    def fake_urlopen(req, timeout):
        seen["body"] = json.loads(req.data.decode())
        return _fake_response({"choices": [{"message": {"content": "ok"}}]})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        llm.complete("p")
    assert seen["body"]["provider"] == {"sort": "price"} and seen["body"]["model"] == "m"


def test_single_rung_http_error_keeps_the_body_too():
    """Even without a chain, the error names the provider's reason (the bare
    'HTTP Error 400: Bad Request' is what row #225 showed)."""
    llm = make_llm({"CFOP_EXEC_LLM_BACKEND": "anthropic", "ANTHROPIC_API_KEY": "sk"})
    with patch("urllib.request.urlopen", side_effect=_http_error(400, "credit balance is too low")):
        with pytest.raises(BackendHTTPError) as ei:
            llm.complete("p")
    assert ei.value.status == 400 and "credit balance" in str(ei.value)


def test_legacy_env_without_chain_is_unchanged():
    assert type(make_llm({"CFOP_EXEC_LLM_BACKEND": "anthropic", "ANTHROPIC_API_KEY": "sk"})).__name__ == "AnthropicLLM"


def test_rung_with_a_named_key_never_borrows_another_providers_key():
    """OPENROUTER_API_KEY absent: the rung is unavailable. It must not send
    CFOP_EXEC_LLM_API_KEY or OPENAI_API_KEY to openrouter.ai instead."""
    llm = make_llm(_chain_env(OPENROUTER_API_KEY="", CFOP_EXEC_LLM_API_KEY="leak-me", OPENAI_API_KEY="leak-me-too"))
    assert llm.llms[1] is None and "OPENROUTER_API_KEY" in llm.build_errors[1]
    # the rung that names no variable keeps the original fallbacks
    plain = [{"backend": "openai", "model": "m", "base_url": "http://ollama:11434/v1"}]
    keyless = make_llm({"CFOP_EXEC_LLM_CHAIN": json.dumps(plain)})
    assert keyless.llms[0].api_key == ""  # keyless is fine for a rung that names nothing
    borrowed = make_llm({"CFOP_EXEC_LLM_CHAIN": json.dumps(plain), "OPENAI_API_KEY": "k"})
    assert borrowed.llms[0].api_key == "k"


def test_chain_falls_through_on_non_json_body_and_truncated_read():
    import http.client
    llm = make_llm(_chain_env())
    html = _fake_response({"choices": [{"message": {"content": "ok"}}]})
    bad = MagicMock(); bad.read.return_value = b"<html>gateway error</html>"
    bad.__enter__ = lambda s: s; bad.__exit__ = lambda s, *a: False
    with patch("urllib.request.urlopen", side_effect=[bad, html]):
        assert llm.complete("p") == "ok"
    assert "not JSON" in llm.describe()["attempts"][0]["error"]
    llm2 = make_llm(_chain_env())
    cut = MagicMock(); cut.read.side_effect = http.client.IncompleteRead(b"partial")
    cut.__enter__ = lambda s: s; cut.__exit__ = lambda s, *a: False
    html2 = _fake_response({"choices": [{"message": {"content": "ok"}}]})  # a fresh body; the first was consumed
    with patch("urllib.request.urlopen", side_effect=[cut, html2]):
        assert llm2.complete("p") == "ok"
    assert "IncompleteRead" in llm2.describe()["attempts"][0]["error"]


def test_chain_attempts_do_not_repeat_across_calls():
    llm = make_llm(_chain_env())
    ok = lambda: _fake_response({"choices": [{"message": {"content": "x"}}]})  # noqa: E731
    with patch("urllib.request.urlopen",
               side_effect=[_http_error(400, "no credit"), ok(), _http_error(400, "no credit"), ok()]):
        llm.complete("pass 1"); llm.complete("pass 2")
    d = llm.describe()
    assert len(d["attempts"]) == 1 and d["attempts"][0]["calls"] == [1, 2] and d["calls"] == 2
