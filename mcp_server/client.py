"""Thin async HTTP client for the agent API.

All methods raise UpstreamError with the plan's structured error vocabulary
(upstream_unavailable / unauthorized / not_found / conflict / validation) so
tools can surface host-neutral failures without leaking transport details.

run_chat() is the shared start-poll-collect orchestration used by both the
ask_sre MCP tool and the Slack bridge's LocalCfopRuntime.
"""

import asyncio

import httpx

_STATUS_MAP = {
    400: ("validation", False),
    401: ("unauthorized", False),
    403: ("unauthorized", False),
    404: ("not_found", False),
    409: ("conflict", False),
    503: ("upstream_unavailable", True),
}


class UpstreamError(Exception):
    def __init__(self, code, message, retryable=False, status=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status = status

    def to_payload(self):
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


class CfopClient:
    def __init__(self, settings, transport=None):
        self._settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.agent_url,
            timeout=settings.request_timeout,
            transport=transport,
        )

    async def aclose(self):
        await self._http.aclose()

    async def _request(self, method, path, **kwargs):
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.TimeoutException as e:
            raise UpstreamError(
                "upstream_unavailable", f"agent request timed out: {e}", retryable=True)
        except httpx.HTTPError as e:
            raise UpstreamError(
                "upstream_unavailable", f"agent unreachable: {e}", retryable=True)

        if resp.status_code >= 400:
            try:
                detail = (resp.json() or {}).get("error") or resp.text[:200]
            except ValueError:
                detail = resp.text[:200]
            code, retryable = _STATUS_MAP.get(
                resp.status_code, ("upstream_unavailable", resp.status_code >= 500))
            raise UpstreamError(code, str(detail), retryable=retryable,
                                status=resp.status_code)
        try:
            return resp.json()
        except ValueError:
            raise UpstreamError(
                "upstream_unavailable", "agent returned a non-JSON response",
                retryable=False, status=resp.status_code)

    # --- triage / investigations ---

    async def triage(self, alert):
        return await self._request("POST", "/v1/triage", json=alert)

    async def start_investigation(self, alert):
        return await self._request("POST", "/v1/investigate", json=alert)

    async def list_investigations(self, limit=50):
        return await self._request("GET", "/api/investigations",
                                   params={"limit": limit})

    async def get_investigation(self, investigation_id):
        return await self._request("GET", f"/api/investigations/{investigation_id}")

    # --- knowledge / digests ---

    async def search_knowledge(self, query, limit=5):
        return await self._request("GET", "/api/kb/search",
                                   params={"q": query, "limit": limit})

    async def list_sweep_reports(self, limit=20):
        return await self._request("GET", "/api/sweep-reports",
                                   params={"limit": limit})

    # --- chat ---

    async def start_chat(self, message, history=None, backend="auto", model=None):
        body = {"message": message, "backend": backend}
        if history:
            body["history"] = history
        if model:
            body["model"] = model
        return await self._request("POST", "/api/chat", json=body)

    async def chat_events(self, chat_id, cursor=0):
        return await self._request("GET", f"/api/chat/events/{chat_id}",
                                   params={"cursor": cursor})

    async def stop_chat(self, chat_id):
        return await self._request("POST", f"/api/chat/{chat_id}/stop")

    async def run_chat(self, message, history=None, timeout=None,
                       poll_interval=None, backend="auto", model=None):
        """Start a chat, poll events until done, return the final reply.

        backend/model select the agent-side LLM per call (backend one of
        auto/ollama/groq/anthropic/xai; model is provider-specific).

        Returns {chat_id, response, tool_calls}. On deadline the chat is
        stopped server-side (best-effort) and a retryable UpstreamError is
        raised so the agent doesn't keep burning LLM cycles for a caller
        that gave up.
        """
        loop = asyncio.get_running_loop()
        timeout = timeout or self._settings.chat_timeout
        poll_interval = poll_interval or self._settings.chat_poll_interval
        deadline = loop.time() + timeout

        started = await self.start_chat(message, history=history,
                                        backend=backend, model=model)
        chat_id = started.get("chat_id")
        if not chat_id:
            raise UpstreamError(
                "upstream_unavailable", "agent returned no chat_id", retryable=True)

        cursor = 0
        tool_calls = 0
        response = ""
        while True:
            batch = await self.chat_events(chat_id, cursor=cursor)
            cursor = batch.get("cursor", cursor)
            for evt in batch.get("events", []):
                kind = evt.get("event")
                data = evt.get("data") or {}
                if kind == "tool_call":
                    tool_calls += 1
                elif kind == "done":
                    response = data.get("response") or ""
                elif kind == "error":
                    raise UpstreamError(
                        "upstream_unavailable",
                        f"agent chat failed: {data.get('error', 'unknown error')}",
                        retryable=True)
            if batch.get("done"):
                break
            if loop.time() >= deadline:
                try:
                    await self.stop_chat(chat_id)
                except UpstreamError:
                    pass
                raise UpstreamError(
                    "upstream_unavailable",
                    f"chat {chat_id} still running after {timeout:.0f}s; stopped it",
                    retryable=True)
            await asyncio.sleep(poll_interval)

        return {"chat_id": chat_id, "response": response, "tool_calls": tool_calls}

    # --- remediations ---

    async def list_remediations(self, status=None, limit=50):
        params = {"limit": limit}
        if status:
            params["status"] = status
        return await self._request("GET", "/api/remediations", params=params)

    async def get_remediation(self, remediation_id):
        return await self._request("GET", f"/api/remediations/{remediation_id}")

    async def approve_remediation(self, remediation_id):
        return await self._request(
            "POST", f"/api/remediations/{remediation_id}/approve")

    async def reject_remediation(self, remediation_id, note=None):
        return await self._request(
            "POST", f"/api/remediations/{remediation_id}/reject",
            json={"note": note} if note else {})
