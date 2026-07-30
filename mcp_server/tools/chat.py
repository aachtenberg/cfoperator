"""ask_sre — synchronous wrapper over the agent's async chat API."""

from mcp_server.tools import guarded


def register(mcp, client, settings):

    @mcp.tool()
    async def ask_sre(
        question: str,
        timeout_seconds: float | None = None,
        backend: str = "auto",
        model: str | None = None,
    ) -> dict:
        """Ask the CFOperator SRE agent a question about the fleet.

        Starts a chat, lets the agent run its tool loop (metrics, logs, k8s,
        SSH — server-side, inside the cluster), and blocks until the final
        reply. Returns {chat_id, response, tool_calls}. Long questions can
        take minutes; raise timeout_seconds if needed.

        backend selects the agent's LLM for this call: auto (default —
        the configured local model with fallback), ollama, groq, anthropic,
        or xai. model optionally names a provider-specific model (e.g.
        backend='anthropic', model='claude-opus-5' for a second opinion on
        a suspect local-model conclusion).
        Requires scope: investigate.
        """
        return await guarded(
            settings, "investigate",
            lambda: client.run_chat(question, timeout=timeout_seconds,
                                    backend=backend, model=model),
            tool="ask_sre")
