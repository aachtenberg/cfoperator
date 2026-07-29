"""LocalCfopRuntime — answer Slack turns with the CFOperator agent itself.

No external coding agent: each turn is one run through the agent's chat API
(the agent does its own tool loop in-cluster). Messages starting with
"investigate:" enqueue an asynchronous investigation instead.
"""

import logging

from mcp_server.client import UpstreamError

logger = logging.getLogger(__name__)

INVESTIGATE_PREFIX = "investigate:"


class LocalCfopRuntime:
    def __init__(self, client, chat_timeout=300.0, max_history_turns=10):
        self._client = client
        self._chat_timeout = chat_timeout
        self._max_history_turns = max_history_turns
        self._history = {}  # thread_id -> [{'role': ..., 'content': ...}]

    async def start(self, prompt, *, thread_id):
        return await self._turn(prompt, thread_id)

    async def follow_up(self, thread_id, prompt):
        return await self._turn(prompt, thread_id)

    async def _turn(self, prompt, thread_id):
        if prompt.lower().startswith(INVESTIGATE_PREFIX):
            summary = prompt[len(INVESTIGATE_PREFIX):].strip()
            if not summary:
                return "Usage: `investigate: <what to investigate>`"
            result = await self._client.start_investigation({
                "summary": summary,
                "source": "slack-bridge",
                # Slack retries deliveries; thread+text keys the dedup window.
                "idempotency_key": f"slack-{thread_id}-{hash(summary) & 0xffffffff:08x}",
            })
            if result.get("status") == "deduped":
                return "Already investigating that (deduped)."
            return (f"Investigation enqueued (queue depth "
                    f"{result.get('queue_depth', '?')}). I'll have findings in "
                    f"the investigations console; ask me here for status.")

        history = self._history.get(thread_id, [])
        result = await self._client.run_chat(
            prompt, history=history, timeout=self._chat_timeout)
        reply = result.get("response") or "(the agent returned an empty response)"

        turns = history + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": reply},
        ]
        # Cap to the last N turns (2 messages per turn) so long threads don't
        # blow the agent's context.
        self._history[thread_id] = turns[-2 * self._max_history_turns:]
        return reply
