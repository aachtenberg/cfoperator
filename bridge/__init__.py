"""Slack bridge for CFOperator — conversational threads via pluggable runtimes.

Slack Socket Mode (outbound websocket, no public ingress) receives mentions
and DMs, hands each turn to an AgentRuntime, and posts the reply back on the
thread. Phase 2 ships LocalCfopRuntime only (agent chat API, no external
coding agent); Cursor/Anthropic runtimes are phase 3.

Outbound alert paging stays on event_runtime webhooks — this bridge owns
conversations only (design rule 6 in docs/mcp-server-plan.md).
"""
