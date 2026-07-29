"""AnthropicRuntime — Claude drives cfoperator's MCP tools to answer Slack turns.

Each turn opens an MCP session to cfoperator-mcp (streamable HTTP, in-cluster),
converts the server's tools to Anthropic tool definitions, and lets Claude run
an agentic tool-use loop until it has an answer. This is the phase-3 exit
criterion in action: the runtime consumes the same MCP server as every other
host — no bespoke agent plumbing in the bridge.

Everything stays inside the cluster except the Anthropic API call itself
(outbound HTTPS); the MCP token never leaves the pod.
"""

import logging

logger = logging.getLogger(__name__)

SYSTEM = """You are CFOperator's Slack assistant for a homelab k3s fleet \
(Raspberry Pis + x86 nodes, ArgoCD GitOps, Prometheus/Loki observability).

Use the provided tools to answer questions: list/get investigations and \
remediations, search_knowledge for past learnings, triage_alert for one-off \
alert assessments, and ask_sre to delegate deep questions to the in-cluster \
SRE agent (it runs its own tool loop over metrics/logs/k8s — slow but \
thorough). Prefer the cheap read tools when they suffice; reach for ask_sre \
when the question needs live fleet data you cannot get otherwise.

You are replying in Slack threads: keep answers concise and conversational, \
use Slack mrkdwn (*bold*, `code`), avoid huge dumps — summarize and offer to \
dig deeper. Never claim fleet state you did not get from a tool this turn."""


class AnthropicRuntime:
    def __init__(self, settings, anthropic_client=None):
        from anthropic import AsyncAnthropic

        self._settings = settings
        # ANTHROPIC_API_KEY from env; client is loop-safe because every turn
        # runs on the bridge's single persistent event loop.
        self._client = anthropic_client or AsyncAnthropic()
        self._history = {}  # thread_id -> [{'role', 'content'}] (text-only turns)

    async def start(self, prompt, *, thread_id):
        return await self._turn(prompt, thread_id)

    async def follow_up(self, thread_id, prompt):
        return await self._turn(prompt, thread_id)

    async def _turn(self, prompt, thread_id):
        history = self._history.get(thread_id, [])
        messages = history + [{"role": "user", "content": prompt}]
        reply = await self._run_agent_loop(messages)
        turns = messages + [{"role": "assistant", "content": reply}]
        self._history[thread_id] = turns[-2 * self._settings.max_history_turns:]
        return reply

    async def _run_agent_loop(self, messages):
        from anthropic.lib.tools.mcp import async_mcp_tool
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        s = self._settings
        async with streamablehttp_client(
            s.mcp_url,
            headers={"Authorization": f"Bearer {s.mcp_token}"},
        ) as (read, write, _):
            async with ClientSession(read, write) as mcp_session:
                await mcp_session.initialize()
                tools_result = await mcp_session.list_tools()

                kwargs = {}
                # Server-side refusal fallbacks: safety-classifier declines
                # re-run on Anthropic's recommended substitute model instead
                # of surfacing an empty reply to Slack.
                if "opus-5" in s.anthropic_model or "fable" in s.anthropic_model:
                    kwargs["betas"] = ["server-side-fallback-2026-07-01"]
                    kwargs["extra_body"] = {"fallbacks": "default"}

                runner = self._client.beta.messages.tool_runner(
                    model=s.anthropic_model,
                    max_tokens=16000,
                    system=SYSTEM,
                    thinking={"type": "adaptive"},
                    max_iterations=s.max_tool_iterations,
                    messages=messages,
                    tools=[async_mcp_tool(t, mcp_session)
                           for t in tools_result.tools],
                    **kwargs,
                )
                last = None
                async for message in runner:
                    last = message

        return _reply_from_message(last)


def _reply_from_message(message):
    if message is None:
        raise RuntimeError("model returned no messages")
    if message.stop_reason == "refusal":
        return (":no_entry_sign: The model declined that request "
                "(safety classifiers). Try rephrasing.")
    text = "".join(b.text for b in message.content if b.type == "text").strip()
    return text or "(the model returned no text)"
