"""Swappable LLM backends for the portable remediation executor.

The executor must not hard-bind to one model or provider, so model invocation
sits behind a tiny ``complete(prompt) -> str`` interface with interchangeable
backends, selected entirely by env:

  CFOP_EXEC_LLM_BACKEND   openai | anthropic | claude-cli   (default: anthropic)
  CFOP_EXEC_LLM_MODEL     model id (backend default otherwise)
  CFOP_EXEC_LLM_BASE_URL  API base (backend default otherwise)
  CFOP_EXEC_LLM_API_KEY   API key (falls back to ANTHROPIC_API_KEY / OPENAI_API_KEY)
  CFOP_EXEC_LLM_MAX_TOKENS, CFOP_EXEC_LLM_TIMEOUT

The ``openai`` backend speaks the OpenAI /chat/completions shape, so it covers
Ollama, vLLM, the homelab llm-gateway, and OpenAI itself with one code path.
Stdlib only (urllib / subprocess) — keeps the executor image minimal and the
component portable.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from typing import Any, Dict, Optional


class LLMError(RuntimeError):
    """Raised when a backend fails to produce a completion."""


def _post_json(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - URL is operator-config
        return json.loads(resp.read().decode("utf-8"))


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
            # content is a list of blocks; concatenate the text blocks.
            parts = [b.get("text", "") for b in data["content"] if b.get("type") == "text"]
            return "".join(parts)
        except (KeyError, TypeError) as e:
            raise LLMError(f"unexpected Anthropic response shape: {e}") from e


class ClaudeCLILLM(LLM):
    """Headless ``claude -p`` — for parity with the read-only worker image."""

    def __init__(self, model: str, timeout: int = 600, allowed_tools: Optional[list] = None):
        self.model = model
        self.timeout = timeout
        self.allowed_tools = allowed_tools or []

    def complete(self, prompt: str) -> str:
        cmd = ["claude", "-p", prompt, "--output-format", "json", "--max-turns", "30"]
        if self.model:
            cmd += ["--model", self.model]
        if self.allowed_tools:
            cmd += ["--permission-mode", "dontAsk", "--allowedTools", *self.allowed_tools]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)  # nosec
        if proc.returncode != 0:
            raise LLMError(f"claude CLI failed ({proc.returncode}): {proc.stderr[:500]}")
        try:
            return str(json.loads(proc.stdout).get("result") or "")
        except ValueError as e:
            raise LLMError(f"claude CLI returned non-JSON: {e}") from e


_ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com"
_ANTHROPIC_DEFAULT_MODEL = "claude-opus-4-8"


def make_llm(env: Dict[str, str]) -> LLM:
    """Build the configured backend from env (see module docstring)."""
    backend = (env.get("CFOP_EXEC_LLM_BACKEND") or "anthropic").strip().lower()
    model = (env.get("CFOP_EXEC_LLM_MODEL") or "").strip()
    base_url = (env.get("CFOP_EXEC_LLM_BASE_URL") or "").strip()
    timeout = int(env.get("CFOP_EXEC_LLM_TIMEOUT", "600") or 600)
    max_tokens = int(env.get("CFOP_EXEC_LLM_MAX_TOKENS", "4096") or 4096)

    if backend == "openai":
        api_key = (env.get("CFOP_EXEC_LLM_API_KEY") or env.get("OPENAI_API_KEY") or "").strip()
        if not base_url:
            raise LLMError("openai backend requires CFOP_EXEC_LLM_BASE_URL")
        return OpenAICompatLLM(base_url, model or "gpt-4o", api_key, timeout, max_tokens)

    if backend == "anthropic":
        api_key = (env.get("CFOP_EXEC_LLM_API_KEY") or env.get("ANTHROPIC_API_KEY") or "").strip()
        if not api_key:
            raise LLMError("anthropic backend requires an API key")
        return AnthropicLLM(base_url or _ANTHROPIC_DEFAULT_BASE,
                            model or _ANTHROPIC_DEFAULT_MODEL, api_key, timeout, max_tokens)

    if backend == "claude-cli":
        return ClaudeCLILLM(model or _ANTHROPIC_DEFAULT_MODEL, timeout)

    raise LLMError(f"unknown CFOP_EXEC_LLM_BACKEND: {backend!r}")
