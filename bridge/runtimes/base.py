"""AgentRuntime protocol — the bridge's only contract with a backend."""

from typing import Protocol


class AgentRuntime(Protocol):
    """One conversational backend. thread_id is the bridge's stable key for a
    Slack thread; runtimes keep whatever per-thread state they need behind it
    (chat history, external agent ids, ...)."""

    async def start(self, prompt: str, *, thread_id: str) -> str:
        """First turn in a thread. Returns the reply text."""
        ...

    async def follow_up(self, thread_id: str, prompt: str) -> str:
        """Subsequent turn in a known thread. Returns the reply text."""
        ...
