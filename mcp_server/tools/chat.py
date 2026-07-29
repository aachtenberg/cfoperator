"""ask_sre — synchronous wrapper over the agent's async chat API."""

import asyncio

from mcp_server.client import UpstreamError
from mcp_server.tools import guarded


def register(mcp, client, settings):

    @mcp.tool()
    async def ask_sre(question: str, timeout_seconds: float | None = None) -> dict:
        """Ask the CFOperator SRE agent a question about the fleet.

        Starts a chat, lets the agent run its tool loop (metrics, logs, k8s,
        SSH — server-side, inside the cluster), and blocks until the final
        reply. Returns {chat_id, response, tool_calls}. Long questions can
        take minutes; raise timeout_seconds if needed.
        Requires scope: investigate.
        """

        async def call():
            loop = asyncio.get_running_loop()
            deadline = loop.time() + (timeout_seconds or settings.chat_timeout)
            started = await client.start_chat(question)
            chat_id = started.get("chat_id")
            if not chat_id:
                raise UpstreamError(
                    "upstream_unavailable", "agent returned no chat_id", retryable=True)

            cursor = 0
            tool_calls = 0
            response = ""
            while True:
                batch = await client.chat_events(chat_id, cursor=cursor)
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
                        await client.stop_chat(chat_id)
                    except UpstreamError:
                        pass
                    raise UpstreamError(
                        "upstream_unavailable",
                        f"chat {chat_id} still running after "
                        f"{timeout_seconds or settings.chat_timeout:.0f}s; stopped it",
                        retryable=True)
                await asyncio.sleep(settings.chat_poll_interval)

            return {"chat_id": chat_id, "response": response, "tool_calls": tool_calls}

        return await guarded(settings, "investigate", call)
