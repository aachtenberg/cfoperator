"""Command-line entrypoint for the portable event runtime."""

from __future__ import annotations

import argparse

from .bootstrap import build_portable_runtime
from .server import serve


def main() -> None:
    """Run the portable event runtime server."""
    parser = argparse.ArgumentParser(description="Portable CFOperator event runtime")
    parser.add_argument("serve", nargs="?", default="serve", help="Run the HTTP server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8080, help="Bind port")
    args = parser.parse_args()

    runtime = build_portable_runtime()
    serve(runtime, host=args.host, port=args.port)


if __name__ == "__main__":
    main()