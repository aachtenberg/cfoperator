"""Entry point: python -m bridge"""

import logging
import os
import sys

from bridge.config import BridgeSettings
from bridge.runtimes.local_cfop import LocalCfopRuntime
from bridge.slack_app import SlackBridge
from mcp_server.client import CfopClient
from mcp_server.config import Settings


def main():
    logging.basicConfig(
        level=os.environ.get("CFOP_BRIDGE_LOG_LEVEL", "INFO").upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = BridgeSettings.from_env()
    except ValueError as e:
        print(f"cfoperator-bridge: {e}", file=sys.stderr)
        return 2

    if settings.runtime == "anthropic":
        from bridge.runtimes.anthropic import AnthropicRuntime
        runtime = AnthropicRuntime(settings)
    else:
        client = CfopClient(Settings(
            agent_url=settings.agent_url,
            chat_timeout=settings.chat_timeout,
        ))
        runtime = LocalCfopRuntime(
            client,
            chat_timeout=settings.chat_timeout,
            max_history_turns=settings.max_history_turns,
            escalation_model=settings.escalation_model,
        )
    SlackBridge(settings, runtime).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
