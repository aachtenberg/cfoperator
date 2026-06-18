"""Unified-diff parsing/application for the portable remediation executor.

Vendored (copied, not imported) from the agent's remediation module so the
executor stays self-contained — no monolith dependency. Pure stdlib. Exact
context matching only: any drift between the LLM's view and the base branch is
a decline, never a guess.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

_SECRET_PATH = re.compile(r"(sealed|secret|\.env|credential|token)", re.I)


def is_secret_path(path: str) -> bool:
    """Refuse to patch anything that looks secret-bearing."""
    return bool(_SECRET_PATH.search(path or ""))


def parse_unified_diff(diff_text: str) -> Optional[Tuple[str, List[tuple]]]:
    """Parse a single-file unified diff into (path, hunks).

    Each hunk is (old_start, old_lines, new_lines). Returns None for anything
    that isn't a clean single-file diff — multi-file diffs are a deliberate
    decline, not a loop.
    """
    if not diff_text:
        return None
    lines = diff_text.splitlines()
    path = None
    hunks: List[dict] = []
    current: Optional[dict] = None
    for line in lines:
        if line.startswith("--- "):
            continue
        if line.startswith("+++ "):
            if path is not None:
                return None  # second file header -> multi-file diff
            raw = line[4:].strip()
            path = raw[2:] if raw.startswith("b/") else raw
            continue
        if line.startswith("@@"):
            m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", line)
            if not m or path is None:
                return None
            current = {"old_start": int(m.group(1)), "old": [], "new": []}
            hunks.append(current)
            continue
        if current is None:
            continue  # preamble text before the first hunk
        if line.startswith(" ") or line == "":
            body = line[1:] if line.startswith(" ") else ""
            current["old"].append(body)
            current["new"].append(body)
        elif line.startswith("-"):
            current["old"].append(line[1:])
        elif line.startswith("+"):
            current["new"].append(line[1:])
        elif line.startswith("\\"):
            continue  # "\ No newline at end of file"
        else:
            return None  # malformed hunk body
    if not path or not hunks:
        return None
    return path, [(h["old_start"], h["old"], h["new"]) for h in hunks]


def apply_unified_diff(text: str, hunks: List[tuple]) -> Optional[str]:
    """Apply parsed hunks with exact context matching; None on any mismatch.

    Tries the hunk's stated position first; falls back to a whole-file search
    only when the expected block occurs exactly once (ambiguous or absent match
    means the file drifted from what the LLM saw — reject, don't guess).
    """
    lines = text.splitlines()
    offset = 0
    for old_start, old, new in hunks:
        if not old:
            return None  # pure-insertion hunks lack anchoring context
        idx = old_start - 1 + offset
        if lines[idx:idx + len(old)] != old:
            matches = [
                i for i in range(len(lines) - len(old) + 1)
                if lines[i:i + len(old)] == old
            ]
            if len(matches) != 1:
                return None
            idx = matches[0]
        lines[idx:idx + len(old)] = new
        offset += len(new) - len(old)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def extract_diff_block(report: str) -> Optional[str]:
    """Return the first ```diff fenced block from an LLM report, if any."""
    m = re.search(r"```diff\n(.*?)```", report or "", re.DOTALL)
    return m.group(1).strip() if m else None
