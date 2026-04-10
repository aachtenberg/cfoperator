"""Command-line entrypoint for the portable event runtime."""

from __future__ import annotations

import argparse
import os
import sys

from .bootstrap import build_portable_runtime, build_portable_worker
from .server import serve


def main() -> None:
    """Run the portable event runtime server."""
    parser = argparse.ArgumentParser(description="Portable CFOperator event runtime")
    subparsers = parser.add_subparsers(dest="command")
    serve_parser = subparsers.add_parser("serve", help="Run the HTTP server")
    serve_parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    serve_parser.add_argument("--port", type=int, default=8080, help="Bind port")
    serve_parser.add_argument("--config", default=None, help="Optional YAML config path")
    serve_parser.add_argument("--poll-interval", type=int, default=30, help="Alert source poll interval in seconds")

    argv = sys.argv[1:]
    if not argv or argv[0].startswith("-"):
        argv = ["serve", *argv]
    args = parser.parse_args(argv)

    if args.command != "serve":
        parser.error(f"Unknown command: {args.command}")

    if args.config:
        os.environ["CONFIG_PATH"] = args.config

    runtime = build_portable_runtime(config_path=args.config)
    worker = build_portable_worker(runtime, config_path=args.config)
    serve(runtime, host=args.host, port=args.port, worker=worker, poll_interval_seconds=args.poll_interval)


if __name__ == "__main__":
    main()