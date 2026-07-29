"""Environment-driven settings for the Slack bridge."""

import os
from dataclasses import dataclass

VALID_RUNTIMES = ("local",)


@dataclass(frozen=True)
class BridgeSettings:
    slack_bot_token: str = ""
    slack_app_token: str = ""
    runtime: str = "local"
    agent_url: str = "http://127.0.0.1:8083"
    chat_timeout: float = 300.0
    max_history_turns: int = 10

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
                f"CFOP_BRIDGE_RUNTIME={runtime!r} invalid; phase 2 supports "
                f"{VALID_RUNTIMES}")
        return cls(
            slack_bot_token=bot,
            slack_app_token=app,
            runtime=runtime,
            agent_url=(env.get("CFOP_AGENT_URL") or cls.agent_url).rstrip("/"),
            chat_timeout=float(env.get("CFOP_BRIDGE_CHAT_TIMEOUT") or cls.chat_timeout),
            max_history_turns=int(
                env.get("CFOP_BRIDGE_MAX_HISTORY_TURNS") or cls.max_history_turns),
        )
