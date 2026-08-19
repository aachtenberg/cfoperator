"""Swappable LLM backends for the discovery pass.

Same contract as the executor's client (the proven portable-component
pattern): model invocation sits behind ``complete(prompt) -> str`` with
interchangeable backends, selected entirely by env:

  CFOP_DISCOVERY_LLM_BACKEND   openai | anthropic          (default: openai)
  CFOP_DISCOVERY_LLM_MODEL     model id (backend default otherwise)
  CFOP_DISCOVERY_LLM_BASE_URL  API base (required for openai)
  CFOP_DISCOVERY_LLM_API_KEY   API key (falls back to ANTHROPIC_API_KEY / OPENAI_API_KEY)
  CFOP_DISCOVERY_LLM_MAX_TOKENS, CFOP_DISCOVERY_LLM_TIMEOUT

The default backend is ``openai`` (the /chat/completions shape covers Ollama,
vLLM, llm-gateway, and OpenAI itself) because the discovery bounds promise a
local model by default — pointing CFOP_DISCOVERY_LLM_BASE_URL at an Ollama
``/v1`` is the expected trial configuration. Stdlib only.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict


class LLMError(RuntimeError):
    """Raised when a backend fails to produce a completion."""


def _post_json(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})
    # Transport failures become LLMError so main() exits 1 with a real message
    # instead of a traceback (an unreachable Ollama is an expected trial mishap).
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - URL is operator-config
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        raise LLMError(f"LLM backend HTTP {e.code}: {detail or e.reason}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LLMError(f"LLM backend request failed: {e}") from e


class LLM:
    """Backend interface: turn a prompt into completion text."""

    def complete(self, prompt: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class OpenAICompatLLM(LLM):
    """OpenAI /chat/completions — also Ollama, vLLM, llm-gateway, etc."""

    def __init__(self, base_url: str, model: str, api_key: str = "",
                 timeout: int = 600, max_tokens: int = 4096):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens

    def complete(self, prompt: str) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        data = _post_json(f"{self.base_url}/chat/completions", headers, body, self.timeout)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"unexpected OpenAI-compat response shape: {e}") from e


class AnthropicLLM(LLM):
    """Anthropic Messages API."""

    def __init__(self, base_url: str, model: str, api_key: str,
                 timeout: int = 600, max_tokens: int = 4096):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens

    def complete(self, prompt: str) -> str:
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        data = _post_json(f"{self.base_url}/v1/messages", headers, body, self.timeout)
        try:
            parts = [b.get("text", "") for b in data["content"] if b.get("type") == "text"]
            return "".join(parts)
        except (KeyError, TypeError) as e:
            raise LLMError(f"unexpected Anthropic response shape: {e}") from e


_ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com"
_ANTHROPIC_DEFAULT_MODEL = "claude-opus-4-8"


def make_llm(env: Dict[str, str]) -> LLM:
    """Build the configured backend from env (see module docstring)."""
    backend = (env.get("CFOP_DISCOVERY_LLM_BACKEND") or "openai").strip().lower()
    model = (env.get("CFOP_DISCOVERY_LLM_MODEL") or "").strip()
    base_url = (env.get("CFOP_DISCOVERY_LLM_BASE_URL") or "").strip()
    timeout = int(env.get("CFOP_DISCOVERY_LLM_TIMEOUT", "600") or 600)
    max_tokens = int(env.get("CFOP_DISCOVERY_LLM_MAX_TOKENS", "4096") or 4096)

    if backend == "openai":
        api_key = (env.get("CFOP_DISCOVERY_LLM_API_KEY") or env.get("OPENAI_API_KEY") or "").strip()
        if not base_url:
            raise LLMError(
                "openai backend requires CFOP_DISCOVERY_LLM_BASE_URL "
                "(e.g. your Ollama http://host:11434/v1), or set "
                "CFOP_DISCOVERY_LLM_BACKEND=anthropic with an API key")
        return OpenAICompatLLM(base_url, model or "gpt-4o", api_key, timeout, max_tokens)

    if backend == "anthropic":
        api_key = (env.get("CFOP_DISCOVERY_LLM_API_KEY") or env.get("ANTHROPIC_API_KEY") or "").strip()
        if not api_key:
            raise LLMError("anthropic backend requires an API key")
        return AnthropicLLM(base_url or _ANTHROPIC_DEFAULT_BASE,
                            model or _ANTHROPIC_DEFAULT_MODEL, api_key, timeout, max_tokens)

    raise LLMError(f"unknown CFOP_DISCOVERY_LLM_BACKEND: {backend!r}")
