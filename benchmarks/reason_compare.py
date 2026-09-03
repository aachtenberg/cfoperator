#!/usr/bin/env python3
"""Print the triage REASON for one eval case, across models, side by side.

Why this exists: triage_eval.py scores `action` and does not persist `reason`
(eval-v2 plan, Tier 3 -- "the harness has to persist reason first"). So the
regression CFOP-153 is about is invisible to the gate. A model emitting four
canned strings scores 42/42.

This reuses the eval's own prompt construction, so what is sent here is what
production sends -- system prompt extracted from agent/agent.py at runtime,
user message built by the same code path.

    PYTHONPATH=agent:. .venv/bin/python benchmarks/reason_compare.py \
        --case known-sdcard \
        --models cfop-triage-ministral3:v2-q4 cfop-triage-ministral3:v1-q4 gemma4:26b

Read the output, do not score it. The question is whether the reason names
THIS alert -- the pod/node, the cited precedent -- or is a frame with filler.
"""
import argparse, json, sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import triage_eval as te  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="known-sdcard")
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--url", default="http://localhost:11434")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=180)
    a = ap.parse_args()

    case = next((c for c in te.CASES if c["name"] == a.case), None)
    if case is None:
        ap.error(f"unknown case {a.case!r}; have: "
                 + ", ".join(c["name"] for c in te.CASES))

    sysp = te.load_production_system_prompt()
    user = te.build_user_message(case)

    print(f"case     : {case['name']}")
    print(f"summary  : {case['summary']}")
    print(f"expected : {case['expected']}")
    print(f"trap     : {case.get('trap')}")
    print("=" * 78)

    for model in a.models:
        for run in range(a.runs):
            text, secs, err = te.call_ollama(a.url, model, sysp, user, a.timeout)
            tag = f"{model}" + (f" [{run}]" if a.runs > 1 else "")
            if err:
                print(f"\n{tag}\n  ERROR {err}")
                continue
            parsed = None
            try:
                parsed = te._parse(text) if hasattr(te, "_parse") else None
            except Exception:
                parsed = None
            if parsed is None:
                try:
                    parsed = json.loads(text[text.index("{"):text.rindex("}") + 1])
                except Exception:
                    parsed = {}
            print(f"\n{tag}   ({secs}s)")
            print(f"  action     : {parsed.get('action')}")
            print(f"  confidence : {parsed.get('confidence')}")
            print(f"  reason     : {parsed.get('reason')}")
            if not parsed:
                print(f"  RAW        : {text[:300]}")


if __name__ == "__main__":
    main()
