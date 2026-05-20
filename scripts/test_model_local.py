#!/usr/bin/env python3
"""
Run CFOperator's own tool-calling tests against a single local Ollama model.

Reuses the T1/T2/T3 definitions from test_tool_calling.py (no duplication),
but targets one model on a single Ollama URL instead of sweeping remote hosts.

Usage:
    python scripts/test_model_local.py qwen3.6:27b [http://localhost:11434]
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_tool_calling as t  # noqa: E402


def main():
    model = sys.argv[1] if len(sys.argv) > 1 else "qwen3.6:27b"
    url = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:11434"

    print(f"Testing {model} @ {url} with CFOperator T1/T2/T3 tool-calling suite\n")
    result = t.test_model("local", url, model)

    print("\n--- detail ---")
    print(f"T1 single call:    [{result['T1_single']['score']}/2] {result['T1_single']['detail']}")
    print(f"T2 multi-turn:     [{result['T2_multi']['score']}/2] {result['T2_multi']['detail']}")
    print(f"T3 tool selection: [{result['T3_select']['score']}/2] {result['T3_select']['detail']}")
    print(f"\nFINAL: {result['score']}/10  (raw {result['raw']}/6, {result['time_s']}s)")

    # non-zero exit if the model can't tool-call at all
    sys.exit(0 if result["raw"] > 0 else 1)


if __name__ == "__main__":
    main()
