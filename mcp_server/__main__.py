"""Entry point: python -m mcp_server"""

import logging
import os
import sys

from mcp_server.config import Settings
from mcp_server.server import run


def main():
    # stdout belongs to the stdio transport — all logging goes to stderr.
    logging.basicConfig(
        level=os.environ.get("CFOP_MCP_LOG_LEVEL", "INFO").upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = Settings.from_env()
    except ValueError as e:
        print(f"cfoperator-mcp: {e}", file=sys.stderr)
        return 2
    run(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
