"""Environment-driven settings for the Slack bridge."""

import os
from dataclasses import dataclass

VALID_RUNTIMES = ("local", "anthropic")


@dataclass(frozen=True)
class BridgeSettings:
    slack_bot_token: str = ""
    slack_app_token: str = ""
    runtime: str = "local"
    agent_url: str = "http://127.0.0.1:8083"
    chat_timeout: float = 300.0
    max_history_turns: int = 10
    # anthropic runtime only
    anthropic_model: str = "claude-opus-5"
    mcp_url: str = "http://cfoperator-mcp.apps.svc.cluster.local:8090/mcp"
    mcp_token: str = ""
    max_tool_iterations: int = 15

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        bot = env.get("SLACK_BOT_TOKEN") or ""
        app = env.get("SLACK_APP_TOKEN") or ""
        if not bot or not app:
            raise ValueError(
                "SLACK_BOT_TOKEN (xoxb-...) and SLACK_APP_TOKEN (xapp-..., "
                "Socket Mode) are both required")
        runtime = (env.get("CFOP_BRIDGE_RUNTIME") or "local").strip().lower()
        if runtime not in VALID_RUNTIMES:
            raise ValueError(
                f"CFOP_BRIDGE_RUNTIME={runtime!r} invalid; supported: "
                f"{VALID_RUNTIMES}")
        mcp_token = env.get("CFOP_MCP_TOKEN") or ""
        if runtime == "anthropic":
            if not mcp_token:
                raise ValueError(
                    "CFOP_MCP_TOKEN is required for the anthropic runtime "
                    "(bearer for cfoperator-mcp)")
            if not env.get("ANTHROPIC_API_KEY"):
                raise ValueError(
                    "ANTHROPIC_API_KEY is required for the anthropic runtime")
        return cls(
            slack_bot_token=bot,
            slack_app_token=app,
            runtime=runtime,
            agent_url=(env.get("CFOP_AGENT_URL") or cls.agent_url).rstrip("/"),
            chat_timeout=float(env.get("CFOP_BRIDGE_CHAT_TIMEOUT") or cls.chat_timeout),
            max_history_turns=int(
                env.get("CFOP_BRIDGE_MAX_HISTORY_TURNS") or cls.max_history_turns),
            anthropic_model=env.get("CFOP_BRIDGE_ANTHROPIC_MODEL") or cls.anthropic_model,
            mcp_url=(env.get("CFOP_MCP_URL") or cls.mcp_url),
            mcp_token=mcp_token,
            max_tool_iterations=int(
                env.get("CFOP_BRIDGE_MAX_TOOL_ITERATIONS") or cls.max_tool_iterations),
        )
