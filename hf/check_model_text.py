#!/usr/bin/env python3
"""Leak gate for a triage fine-tune: does the model say things it must not?

The dataset scan (hf/scan_dataset.py) checks what went IN to training. This
checks what comes OUT. It replays prompts through the served model and fails
if any output contains a forbidden string (the operator's domain, passed on
the command line and never stored) or anything the dataset scanner's
patterns would flag (tokens, emails, public addresses, ...).

Which prompts:
  --dataset FILE...   rows from a scrubbed train/val JSONL. By default only the
                      rows the scrub touched (meta.scrub present), because
                      those are the prompts whose ORIGINAL targets carried the
                      domain: if the weights still hold it, this is where it
                      surfaces. --all-rows replays every row.
  --eval-cases        the 14 cases in benchmarks/triage_eval.py, through the
                      production system prompt. Needs PYTHONPATH=agent:. like
                      the eval itself.

Each prompt runs --runs times, because a leak that surfaces one time in ten
is still a leak. Temperature: by default the request mirrors production's
default path and triage_eval.py, which send ``temperature`` at the top level
of the /api/chat body. Ollama reads sampling options only from ``options``,
so what actually applies is the Modelfile's value (0.15 for the triage
tags). ``--temperature 0.7`` puts the value in ``options`` instead, which is
what production sends when ``llm.num_ctx`` is configured, and is the
stricter setting for a leak gate: hotter sampling surfaces more of what the
weights hold. Run both.

Usage (on the ollama host, after `ollama create`):
    PYTHONPATH=agent:. .venv/bin/python hf/check_model_text.py \\
        --model cfop-triage-ministral3:v6-q4 --forbid <operator-domain> \\
        --dataset /mnt/nas-backup/unsloth/cfoperator-v7/triage_train.jsonl \\
        --eval-cases --runs 10

Exit status: 0 clean; 1 at least one hit (listed, with the forbidden string
itself redacted); 2 bad arguments (no --forbid, --runs < 1), unreachable
model, or no prompts. A gate that checks nothing does not report CLEAN.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent


def _load_scanner():
    spec = importlib.util.spec_from_file_location("scan_dataset", HERE / "scan_dataset.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def build_payload(model: str, system_prompt: str, user_msg: str, temperature: float | None) -> dict:
    """The /api/chat body. With ``temperature`` None this is byte-for-byte the
    shape triage_eval.call_ollama and production's default path send (top-level
    ``temperature``, which ollama ignores); with a value it goes in ``options``,
    the only place ollama reads it, as production does on its num_ctx path."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "temperature": 0.7,
    }
    if temperature is not None:
        payload["options"] = {"temperature": temperature}
    return payload


def call_ollama(url: str, model: str, system_prompt: str, user_msg: str, timeout: int, temperature: float | None = None) -> tuple[str, str | None]:
    """(response_text, error)."""
    payload = build_payload(model, system_prompt, user_msg, temperature)
    req = urllib.request.Request(f"{url}/api/chat", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return "", f"{type(exc).__name__}: {exc}"
    return data.get("message", {}).get("content", ""), None


def dataset_prompts(paths: list[Path], all_rows: bool) -> list[tuple[str, str, str]]:
    """(label, system, user) per selected row."""
    out = []
    for p in paths:
        with p.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if not all_rows and "scrub" not in (row.get("meta") or {}):
                    continue
                msgs = {m["role"]: m["content"] for m in row.get("messages", []) if "role" in m}
                if "system" not in msgs or "user" not in msgs:
                    continue
                out.append((f"{p.name}:{lineno}", msgs["system"], msgs["user"]))
    return out


def eval_prompts() -> list[tuple[str, str, str]]:
    sys.path.insert(0, str(REPO_ROOT / "benchmarks"))
    import triage_eval  # noqa: E402  (imports agent.agent; needs PYTHONPATH=agent:.)

    system_prompt = triage_eval.load_production_system_prompt()
    return [(f"eval:{c['name']}", system_prompt, triage_eval.build_user_message(c)) for c in triage_eval.CASES]


def _redact(s: str) -> str:
    # Nothing of the string itself: the forbidden strings are exactly what must
    # not land in a log. Length is enough to tell two of them apart.
    return f"<{len(s)} chars>"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True)
    ap.add_argument("--url", default="http://localhost:11434")
    ap.add_argument("--forbid", action="append", default=[], help="string that must not appear in any output (case-insensitive); repeatable")
    ap.add_argument("--dataset", nargs="*", type=Path, default=[])
    ap.add_argument("--all-rows", action="store_true", help="replay every dataset row, not only the scrubbed ones")
    ap.add_argument("--eval-cases", action="store_true")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--temperature", type=float, default=None,
                    help="send this sampling temperature in options (ollama honours it there); default: the Modelfile's value applies")
    args = ap.parse_args(argv)

    scanner = _load_scanner()
    forbidden = [f.strip().lower() for f in args.forbid if f.strip()]
    if not forbidden:
        print("no --forbid string given: the gate would check nothing and report CLEAN", file=sys.stderr)
        return 2
    if args.runs < 1:
        print("--runs must be at least 1", file=sys.stderr)
        return 2

    prompts: list[tuple[str, str, str]] = []
    try:
        prompts += dataset_prompts(args.dataset, args.all_rows)
    except (OSError, ValueError) as exc:
        print(f"cannot read dataset: {exc}", file=sys.stderr)
        return 2
    if args.eval_cases:
        prompts += eval_prompts()
    if not prompts:
        print("no prompts selected (no scrubbed rows in --dataset and no --eval-cases)", file=sys.stderr)
        return 2

    hits = 0
    calls = 0
    started = time.monotonic()
    temp = "Modelfile default" if args.temperature is None else f"options.temperature={args.temperature}"
    print(f"model {args.model}: {len(prompts)} prompts x {args.runs} runs, {len(forbidden)} forbidden string(s), {temp}")
    for label, system_prompt, user_msg in prompts:
        for run in range(1, args.runs + 1):
            text, err = call_ollama(args.url, args.model, system_prompt, user_msg, args.timeout, args.temperature)
            calls += 1
            if err:
                print(f"{label} run {run}: model error: {err}", file=sys.stderr)
                return 2
            low = text.lower()
            for f in forbidden:
                if f in low:
                    hits += 1
                    print(f"HIT {label} run {run}: forbidden string {_redact(f)} in output")
            for name, hit in scanner.scan_text(text):
                hits += 1
                print(f"HIT {label} run {run}: scanner {name}: {hit}")
    elapsed = time.monotonic() - started
    print(f"{calls} completions in {elapsed:.0f}s: {'CLEAN' if hits == 0 else f'{hits} hit(s)'}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
