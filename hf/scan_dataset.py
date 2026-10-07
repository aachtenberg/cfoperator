#!/usr/bin/env python3
"""Secret scan for a triage fine-tuning dataset before its weights are published.

The dataset itself is never published (real homelab investigation history,
gitignored for that reason). But the model trained on it is, and a fine-tune
can reproduce what it was trained on. Response-only loss means the weights
were only ever pushed toward the assistant turns, and those are short JSON
verdicts -- but the verdicts quote the prompt (pod names, node names, the
alert summary), so anything secret-shaped that a summary or precedent block
carried in from a log line can come back out of the model. This scan is the
gate: it reads every string in every row, prompts included, and fails on
anything that looks like a credential, an email, a public address or a URL
with userinfo.

It is deliberately dumb and deliberately strict. A false positive costs a
minute of reading; a false negative is a secret in a public repo forever.

Usage:
    python3 hf/scan_dataset.py /mnt/nas-backup/unsloth/cfoperator-v6/triage_train.jsonl \
                               /mnt/nas-backup/unsloth/cfoperator-v6/triage_val.jsonl

Exit status 0 means clean; 1 means at least one finding (listed on stdout,
with the matched text redacted to its first and last four characters); 2
means a file could not be read, decoded or parsed.
"""

from __future__ import annotations

import ipaddress
import json
import re
import sys
from pathlib import Path

# Each pattern is (name, compiled regex). Order is cosmetic.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Vendor-prefixed tokens: Hugging Face, GitHub, OpenAI/Anthropic/Groq-style,
    # Slack, AWS access keys, Google API keys.
    ("hf-token", re.compile(r"\bhf_[A-Za-z0-9]{20,}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("sk-token", re.compile(r"\bsk-(?:ant-|proj-|or-v1-)?[A-Za-z0-9_-]{20,}\b")),
    ("gsk-token", re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google-api-key", re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b")),
    # JWTs: three base64url segments, the first decoding to a JSON header.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    # key=value style assignments and HTTP auth headers.
    # No leading \b: POSTGRES_PASSWORD=... has a word character before the
    # keyword, and that is the common shape in env dumps and log lines.
    # "token" is in the group on purpose: it over-matches (token counts, token
    # budgets) and that is the strict side to err on.
    ("password-assignment", re.compile(r"(?i)(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\w*\s*[:=]\s*['\"]?[^\s'\",]{6,}")),
    ("bearer-header", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("token-header", re.compile(r"(?i)\bauthorization:\s*token\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("basic-auth-header", re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/=]{16,}")),
    # Private key material.
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # URLs carrying userinfo (scheme://user:pass@host).
    ("url-with-userinfo", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@[^\s/]+")),
    # Email addresses. Anything here is a person; the dataset has no reason to carry one.
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
]

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _is_public_ipv4(text: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(text)
    except ipaddress.AddressValueError:
        return False
    # RFC1918, loopback, link-local, CGNAT, multicast, reserved, and the
    # 0.0.0.0/8 and documentation ranges all count as non-public. Version
    # strings such as 1.2.3.4 that happen to parse are the one known false
    # positive, and they are rare enough in alert text to read by hand.
    return addr.is_global


def _redact(s: str) -> str:
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}…{s[-4:]}"


def _strings(obj) -> list[str]:
    """Every string in a JSON value, depth-first: leaves and dict keys alike."""
    out: list[str] = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out.append(k)
            out.extend(_strings(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_strings(v))
    return out


def scan_text(text: str) -> list[tuple[str, str]]:
    """Findings in one string as (pattern-name, redacted-match)."""
    findings: list[tuple[str, str]] = []
    for name, rx in PATTERNS:
        for m in rx.finditer(text):
            findings.append((name, _redact(m.group(0))))
    for m in _IPV4.finditer(text):
        if _is_public_ipv4(m.group(0)):
            findings.append(("public-ipv4", m.group(0)))
    return findings


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Findings in a JSONL file as (line-number, pattern-name, redacted-match)."""
    findings: list[tuple[int, str, str]] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            for s in _strings(row):
                for name, hit in scan_text(s):
                    findings.append((lineno, name, hit))
    return findings


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    total = 0
    for arg in argv:
        path = Path(arg)
        try:
            findings = scan_file(path)
            with path.open(encoding="utf-8") as fh:
                rows = sum(1 for line in fh if line.strip())
        except (OSError, ValueError) as exc:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
            print(f"{path}: cannot scan: {exc}")
            return 2
        if findings:
            print(f"{path}: {len(findings)} finding(s) in {rows} rows")
            for lineno, name, hit in findings:
                print(f"  line {lineno}: {name}: {hit}")
        else:
            print(f"{path}: clean ({rows} rows)")
        total += len(findings)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
