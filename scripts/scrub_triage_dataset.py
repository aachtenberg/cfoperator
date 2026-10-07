#!/usr/bin/env python3
"""Rewrite hostnames under one domain to a placeholder, in a triage dataset.

Why this exists: the v5 triage set (CFOP-153) was built from real alert
history, and one recurring alert named an ingress hostname under the
operator's own domain. Everything else the set names is already in the
public repo; that domain is not, and a fine-tune trained on it will say it
back (CFOP-274, CFOP-277). The fix is one edit on the exact v5 files, not a
rebuild from the database: a rebuild would pull in weeks of new history and
make v5-vs-v6 a comparison of two datasets instead of one hostname.

What it does: for every row, every string (dict keys included), any
``<labels>.<domain>`` becomes ``<labels>.<placeholder>`` and a bare
``<domain>`` becomes ``<placeholder>``. The leftmost labels are kept, so
``freshet.<domain>`` stays recognisably "the freshet ingress" and the row
keeps the shape the model is meant to learn. Touched rows get
``meta.scrub = {"replacements": n, "placeholder": "<placeholder>"}``; the
domain itself is never written anywhere. Untouched rows are copied byte for
byte, so a diff of input and output shows only the edit.

Then it checks its own work: the output is re-read and must not contain the
domain, case-insensitively, anywhere. It also refuses to produce an output
that needed no edits, because a typo in ``--domain`` would otherwise look
like success.

Usage:
    python3 scripts/scrub_triage_dataset.py --domain <operator-domain> \\
        --in-dir /mnt/nas-backup/unsloth/cfoperator-v6 \\
        --out-dir /mnt/nas-backup/unsloth/cfoperator-v7

The placeholder defaults to ``homelab.example``: ``.example`` is reserved by
RFC 2606, so it can never collide with a real host.

Exit status: 0 scrubbed and verified; 1 residual found after scrubbing (the
output is removed); 2 bad arguments or unreadable input; 3 nothing to scrub.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

DEFAULT_FILES = ("triage_train.jsonl", "triage_val.jsonl")
DEFAULT_PLACEHOLDER = "homelab.example"


def _pattern(domain: str) -> re.Pattern[str]:
    # Optional leading labels, then the domain, bounded so that
    # "notxgrunt.com" and "xgrunt.com.evil" are not touched.
    d = re.escape(domain)
    return re.compile(rf"(?<![A-Za-z0-9.-])((?:[A-Za-z0-9-]+\.)*){d}(?![A-Za-z0-9-])", re.IGNORECASE)


def scrub_value(obj, rx: re.Pattern[str], placeholder: str):
    """Return (scrubbed copy, replacement count) for any JSON value."""
    if isinstance(obj, str):
        new, n = rx.subn(lambda m: f"{m.group(1)}{placeholder}", obj)
        return new, n
    if isinstance(obj, list):
        total = 0
        out = []
        for v in obj:
            nv, n = scrub_value(v, rx, placeholder)
            out.append(nv)
            total += n
        return out, total
    if isinstance(obj, dict):
        total = 0
        out = {}
        for k, v in obj.items():
            nk, n1 = scrub_value(k, rx, placeholder)
            nv, n2 = scrub_value(v, rx, placeholder)
            out[nk] = nv
            total += n1 + n2
        return out, total
    return obj, 0


def scrub_file(src: Path, dst: Path, domain: str, placeholder: str) -> tuple[int, int, int]:
    """Scrub src into dst. Returns (rows, rows_touched, replacements)."""
    rx = _pattern(domain)
    rows = touched = replacements = 0
    with src.open(encoding="utf-8") as fin, dst.open("w", encoding="utf-8") as fout:
        for line in fin:
            if not line.strip():
                fout.write(line)
                continue
            rows += 1
            row = json.loads(line)
            new_row, n = scrub_value(row, rx, placeholder)
            if n == 0:
                fout.write(line)  # byte for byte
                continue
            touched += 1
            replacements += n
            meta = new_row.setdefault("meta", {})
            if isinstance(meta, dict):
                meta["scrub"] = {"replacements": n, "placeholder": placeholder}
            fout.write(json.dumps(new_row) + "\n")
    return rows, touched, replacements


def residual(path: Path, domain: str) -> int:
    """Occurrences of the domain left in a file, case-insensitive, any position."""
    return len(re.findall(re.escape(domain), path.read_text(encoding="utf-8"), re.IGNORECASE))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domain", required=True, help="the domain to remove, e.g. example.com (never written to the output)")
    ap.add_argument("--placeholder", default=DEFAULT_PLACEHOLDER, help=f"what it becomes (default {DEFAULT_PLACEHOLDER})")
    ap.add_argument("--in-dir", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--files", nargs="+", default=list(DEFAULT_FILES))
    args = ap.parse_args(argv)

    domain = args.domain.strip().lower().strip(".")
    if not domain or "." not in domain:
        print(f"--domain {args.domain!r} does not look like a domain", file=sys.stderr)
        return 2
    if domain in args.placeholder.lower():
        print("the placeholder must not contain the domain", file=sys.stderr)
        return 2
    args.out_dir.mkdir(parents=True, exist_ok=True)

    total_replacements = 0
    outputs: list[Path] = []
    for name in args.files:
        src, dst = args.in_dir / name, args.out_dir / name
        if not src.is_file():
            print(f"{src}: not found", file=sys.stderr)
            return 2
        if src.resolve() == dst.resolve():
            print(f"{src}: --out-dir must differ from --in-dir (the input is the record of what was scrubbed)", file=sys.stderr)
            return 2
        try:
            rows, touched, n = scrub_file(src, dst, domain, args.placeholder)
        except (OSError, ValueError) as exc:
            print(f"{src}: {exc}", file=sys.stderr)
            return 2
        outputs.append(dst)
        total_replacements += n
        print(f"{name}: {rows} rows, {touched} touched, {n} replacements")

    bad = False
    for dst in outputs:
        left = residual(dst, domain)
        if left:
            print(f"{dst}: {left} residual occurrence(s) after scrubbing; removing the output", file=sys.stderr)
            dst.unlink(missing_ok=True)
            bad = True
    if bad:
        return 1
    if total_replacements == 0:
        print("nothing matched the domain: no output is any different from its input. Check --domain.", file=sys.stderr)
        for dst in outputs:
            dst.unlink(missing_ok=True)
        return 3

    for dst in outputs:
        print(f"{sha256(dst)}  {dst.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
