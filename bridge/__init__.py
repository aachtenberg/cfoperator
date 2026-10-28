"""Slack bridge for CFOperator — conversational threads via pluggable runtimes.

Slack Socket Mode (outbound websocket, no public ingress) receives mentions
and DMs, hands each turn to an AgentRuntime, and posts the reply back on the
thread. LocalCfopRuntime (agent chat API, no external coding agent) is the
default; AnthropicRuntime (Claude over cfoperator-mcp's tools) arrived in
phase 3, and a Cursor runtime was deferred.

Outbound alert paging stays on event_runtime webhooks — this bridge owns
conversations only (a design decision recorded in docs/mcp-server.md).
"""
