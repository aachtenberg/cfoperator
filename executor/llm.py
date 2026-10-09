"""Swappable LLM backends for the portable remediation executor.

The executor must not hard-bind to one model or provider, so model invocation
sits behind a tiny ``complete(prompt) -> str`` interface with interchangeable
backends, selected entirely by env.

One rung (the original shape, still supported as-is):

  CFOP_EXEC_LLM_BACKEND   openai | anthropic | claude-cli   (default: anthropic)
  CFOP_EXEC_LLM_MODEL     model id (backend default otherwise)
  CFOP_EXEC_LLM_BASE_URL  API base (backend default otherwise)
  CFOP_EXEC_LLM_API_KEY   API key (falls back to ANTHROPIC_API_KEY / OPENAI_API_KEY)
  CFOP_EXEC_LLM_MAX_TOKENS, CFOP_EXEC_LLM_TIMEOUT

A chain of rungs, tried in order until one answers:

  CFOP_EXEC_LLM_CHAIN     JSON list, primary first. Each rung:
                            {"backend": "anthropic" | "openai" | "claude-cli",
                             "model": "...", "base_url": "...",
                             "api_key_env": "NAME_OF_ENV_VAR_HOLDING_THE_KEY",
                             "extra_body": {...}}        # merged into the request
                          Keys are read from the env var the rung names, so the
                          Job passes secrets as secretKeyRef env entries and the
                          chain itself holds no secret.

A rung is skipped, and the next one tried, when the request fails: an HTTP
error of any status (the response body is kept, so a billing or model error
reads as what it is), an unreachable endpoint, a timeout, a response of the
wrong shape, or a rung that cannot even be built (no key in its env var). The
chain records which rung answered and why the earlier ones did not, and the
executor puts that on the completion so the row shows which model wrote the
change. If every rung fails, one error names each rung's reason.

Why a chain and not a retry: the failures this exists for (an exhausted
credit balance, a retired model id, a provider outage) do not heal on retry.
The next rung is a different bill or a different provider.

The ``openai`` backend speaks the OpenAI /chat/completions shape, so it covers
Ollama, vLLM, the homelab llm-gateway, OpenRouter and OpenAI itself with one
code path; ``extra_body`` carries what a particular host wants on top (for
OpenRouter, its provider-routing object). Stdlib only (urllib / subprocess),
which keeps the executor image minimal and the component portable.
"""

from __future__ import annotations

import http.client
import json
import socket
import subprocess
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional


class LLMError(RuntimeError):
    """Raised when a backend fails to produce a completion."""


class BackendHTTPError(LLMError):
    """The backend answered with an HTTP error; ``body`` is what it said."""

    def __init__(self, status: int, body: str, url: str = ""):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"HTTP {status}: {body}" if body else f"HTTP {status}")


class BackendUnreachable(LLMError):
    """No HTTP answer at all: connection refused, DNS, timeout."""


def _post_json(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - URL is operator-config
            raw = resp.read()
    except urllib.error.HTTPError as e:
        # urllib's str() is "HTTP Error 400: Bad Request" and drops the body,
        # which is where "credit balance is too low" lives. Keep it.
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - the body is a courtesy, not a requirement
            detail = ""
        raise BackendHTTPError(e.code, " ".join(detail.split())[:400], url) from e
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError,
            http.client.HTTPException) as e:
        # HTTPException covers a body cut short mid-read (IncompleteRead),
        # which is neither an HTTPError nor an OSError.
        raise BackendUnreachable(f"{url}: {e}") from e
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as e:
        # A 200 that is not JSON (a proxy or gateway error page) is a bad
        # answer from this rung, not a reason to stop the chain.
        raise LLMError(f"{url}: response is not JSON: {e}") from e


class LLM:
    """Backend interface: turn a prompt into completion text."""

    backend: str = ""
    model: str = ""

    def complete(self, prompt: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def describe(self) -> Dict[str, Any]:
        """What answered: backend and model. The chain adds rung and attempts."""
        return {"backend": self.backend, "model": self.model}


class OpenAICompatLLM(LLM):
    """OpenAI /chat/completions — also Ollama, vLLM, llm-gateway, OpenRouter, etc."""

    backend = "openai"

    def __init__(self, base_url: str, model: str, api_key: str = "",
                 timeout: int = 600, max_tokens: int = 4096,
                 extra_body: Optional[Dict[str, Any]] = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.extra_body = dict(extra_body or {})

    def complete(self, prompt: str) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {
            **self.extra_body,
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

    backend = "anthropic"

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

    backend = "claude-cli"

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
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)  # nosec
        except (subprocess.TimeoutExpired, OSError) as e:
            raise BackendUnreachable(f"claude CLI: {e}") from e
        if proc.returncode != 0:
            raise LLMError(f"claude CLI failed ({proc.returncode}): {proc.stderr[:500]}")
        try:
            return str(json.loads(proc.stdout).get("result") or "")
        except ValueError as e:
            raise LLMError(f"claude CLI returned non-JSON: {e}") from e


class ChainLLM(LLM):
    """Try rungs in order; the first that answers wins.

    ``attempts`` holds one entry per rung-and-reason that did not answer,
    across every ``complete`` call, each with the 1-based ``calls`` it failed
    on (the gitops flow makes two calls: 1 picks the file, 2 writes the diff;
    node-action planning makes one). ``rung`` is the index of the rung that
    answered the most recent call. ``describe()`` returns all of it, with the
    answering rung's backend and model.
    """

    backend = "chain"

    def __init__(self, rungs: List[Dict[str, Any]], llms: List[Optional[LLM]],
                 build_errors: List[Optional[str]]):
        if len(rungs) != len(llms) or len(rungs) != len(build_errors):
            raise ValueError("rungs, llms and build_errors must line up")
        if not any(llm is not None for llm in llms):
            raise LLMError("no usable rung in CFOP_EXEC_LLM_CHAIN: "
                           + "; ".join(f"{_rung_name(r)}: {e}" for r, e in zip(rungs, build_errors)))
        self.rungs = rungs
        self.llms = llms
        self.build_errors = build_errors
        self.rung: Optional[int] = None
        self.attempts: List[Dict[str, Any]] = []
        self.calls = 0

    @property
    def model(self) -> str:  # type: ignore[override]
        llm = self.llms[self.rung] if self.rung is not None else None
        return llm.model if llm is not None else ""

    def complete(self, prompt: str) -> str:
        self.calls += 1
        errors: List[str] = []
        for idx, (rung, llm, build_err) in enumerate(zip(self.rungs, self.llms, self.build_errors)):
            name = _rung_name(rung)
            if llm is None:
                self._record(idx, rung, f"unavailable: {build_err}")
                errors.append(f"{name}: unavailable: {build_err}")
                continue
            try:
                text = llm.complete(prompt)
            except (LLMError, OSError) as e:
                self._record(idx, rung, str(e))
                errors.append(f"{name}: {e}")
                continue
            self.rung = idx
            return text
        raise LLMError(f"all {len(self.rungs)} rung(s) failed: " + "; ".join(errors))

    def _record(self, idx: int, rung: Dict[str, Any], reason: str) -> None:
        # A run makes several completions (the gitops flow makes two); a dead
        # primary is one fact, so one entry per rung-and-reason, and ``calls``
        # says which completions it failed on.
        for entry in self.attempts:
            if entry["rung"] == idx and entry["error"] == reason[:500]:
                entry["calls"].append(self.calls)
                return
        self.attempts.append({
            "rung": idx,
            "backend": str(rung.get("backend") or ""),
            "model": str(rung.get("model") or ""),
            "error": reason[:500],
            "calls": [self.calls],
        })

    def describe(self) -> Dict[str, Any]:
        llm = self.llms[self.rung] if self.rung is not None else None
        return {
            "backend": llm.backend if llm is not None else "",
            "model": llm.model if llm is not None else "",
            "rung": self.rung,
            "rungs": len(self.rungs),
            "calls": self.calls,
            "attempts": [dict(a, calls=list(a["calls"])) for a in self.attempts],
        }


def _rung_name(rung: Dict[str, Any]) -> str:
    return f"{rung.get('backend') or '?'}/{rung.get('model') or '?'}"


_ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com"
_ANTHROPIC_DEFAULT_MODEL = "claude-opus-4-8"
_DEFAULT_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def _rung_api_key(rung: Dict[str, Any], env: Dict[str, str]) -> str:
    """The key a rung may send.

    A rung that names its variable (api_key_env) gets that variable and
    nothing else: with the key absent, the answer is "", and the rung is
    skipped as unavailable. Falling through to CFOP_EXEC_LLM_API_KEY or the
    backend default would send another provider's key to this rung's host.
    Only a rung that names no variable uses those two, which is the original
    single-rung behaviour.
    """
    own = str(rung.get("api_key_env") or "").strip()
    if own:
        return (env.get(own) or "").strip()
    for name in ("CFOP_EXEC_LLM_API_KEY", _DEFAULT_KEY_ENV.get(str(rung.get("backend") or ""), "")):
        if name and (env.get(name) or "").strip():
            return env[name].strip()
    return ""


def _build_openai(rung: Dict[str, Any], env: Dict[str, str], timeout: int, max_tokens: int) -> LLM:
    base_url = str(rung.get("base_url") or "").strip()
    if not base_url:
        raise LLMError("openai backend requires base_url (CFOP_EXEC_LLM_BASE_URL)")
    extra = rung.get("extra_body") if isinstance(rung.get("extra_body"), dict) else None
    api_key = _rung_api_key(rung, env)
    if rung.get("api_key_env") and not api_key:
        # The rung said which key it uses and the Job has none: unavailable,
        # not "try without". (A rung naming no variable may legitimately run
        # keyless, e.g. Ollama or the homelab gateway.)
        raise LLMError(f"openai backend rung has no key in {rung.get('api_key_env')}")
    return OpenAICompatLLM(base_url, str(rung.get("model") or "gpt-4o"), api_key,
                           timeout, max_tokens, extra_body=extra)


def _build_anthropic(rung: Dict[str, Any], env: Dict[str, str], timeout: int, max_tokens: int) -> LLM:
    api_key = _rung_api_key(rung, env)
    if not api_key:
        raise LLMError(f"anthropic backend requires an API key "
                       f"(no value in {rung.get('api_key_env') or 'ANTHROPIC_API_KEY'})")
    return AnthropicLLM(str(rung.get("base_url") or "").strip() or _ANTHROPIC_DEFAULT_BASE,
                        str(rung.get("model") or "").strip() or _ANTHROPIC_DEFAULT_MODEL,
                        api_key, timeout, max_tokens)


def _build_claude_cli(rung: Dict[str, Any], env: Dict[str, str], timeout: int, max_tokens: int) -> LLM:
    return ClaudeCLILLM(str(rung.get("model") or "").strip() or _ANTHROPIC_DEFAULT_MODEL, timeout)


# Adding a provider is one function here. The chain does not know their names.
BACKENDS: Dict[str, Callable[[Dict[str, Any], Dict[str, str], int, int], LLM]] = {
    "openai": _build_openai,
    "anthropic": _build_anthropic,
    "claude-cli": _build_claude_cli,
}


def build_rung(rung: Dict[str, Any], env: Dict[str, str], timeout: int, max_tokens: int) -> LLM:
    backend = str(rung.get("backend") or "anthropic").strip().lower()
    builder = BACKENDS.get(backend)
    if builder is None:
        raise LLMError(f"unknown CFOP_EXEC_LLM_BACKEND: {backend!r}")
    return builder(rung, env, timeout, max_tokens)


def parse_chain(raw: str) -> List[Dict[str, Any]]:
    """CFOP_EXEC_LLM_CHAIN as a list of rung dicts; errors name what is wrong."""
    try:
        chain = json.loads(raw)
    except ValueError as e:
        raise LLMError(f"CFOP_EXEC_LLM_CHAIN is not valid JSON: {e}") from e
    if not isinstance(chain, list) or not chain:
        raise LLMError("CFOP_EXEC_LLM_CHAIN must be a non-empty JSON list of rungs")
    for i, rung in enumerate(chain):
        if not isinstance(rung, dict):
            raise LLMError(f"CFOP_EXEC_LLM_CHAIN rung {i} is not an object")
    return chain


def make_llm(env: Dict[str, str]) -> LLM:
    """Build the configured backend, or chain of backends, from env (see module docstring)."""
    timeout = int(env.get("CFOP_EXEC_LLM_TIMEOUT", "600") or 600)
    max_tokens = int(env.get("CFOP_EXEC_LLM_MAX_TOKENS", "4096") or 4096)

    raw_chain = (env.get("CFOP_EXEC_LLM_CHAIN") or "").strip()
    if raw_chain:
        rungs = parse_chain(raw_chain)
        llms: List[Optional[LLM]] = []
        errors: List[Optional[str]] = []
        for rung in rungs:
            try:
                llms.append(build_rung(rung, env, timeout, max_tokens))
                errors.append(None)
            except LLMError as e:
                # A rung that cannot be built is skipped, not fatal: the chain
                # exists so one missing key does not stop the others.
                llms.append(None)
                errors.append(str(e))
        return ChainLLM(rungs, llms, errors)

    # One rung from the original variables: same objects, same errors as before.
    rung = {
        "backend": (env.get("CFOP_EXEC_LLM_BACKEND") or "anthropic").strip().lower(),
        "model": (env.get("CFOP_EXEC_LLM_MODEL") or "").strip(),
        "base_url": (env.get("CFOP_EXEC_LLM_BASE_URL") or "").strip(),
    }
    return build_rung(rung, env, timeout, max_tokens)
