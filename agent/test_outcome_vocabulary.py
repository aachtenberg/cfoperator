#!/usr/bin/env python3
"""The investigation prompts and the investigations.outcome schema must agree.

CFOP-20: 'needs_action' was a status every investigation prompt could emit, but
it was in neither VALID_OUTCOMES nor the valid_outcome CHECK constraint. So
normalize_outcome() fell through to its default and *every* needs_action
investigation was persisted as 'monitoring' — invisible, because the paging path
reads the in-process value and never the stored row.

These tests guard the class of defect rather than today's vocabulary: they
discover the STATUS vocabulary from the prompts themselves, so adding a fifth
status to a prompt without making it storable fails here instead of silently
mislabelling rows in production.
"""

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from knowledge_base import (
    OUTCOME_ALIASES,
    OUTCOME_CHECK_SQL,
    VALID_OUTCOMES,
    Investigation,
    normalize_outcome,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# The prompts declare their vocabulary literally, e.g.
#   STATUS: <one of: resolved | needs_action | monitoring | escalate>
_STATUS_VOCAB = re.compile(r"STATUS:\s*<one of:\s*([^>]+)>")

_SKIP_DIRS = {".git", ".claude", "node_modules", "__pycache__", ".venv", "venv"}


def _prompt_vocabularies():
    """Every declared STATUS vocabulary in the repo, as (source, [tokens]).

    Read as text, never imported: the worker's prompts are markdown templates
    and importing its entrypoint from here would drag in the whole worker tree.
    """
    found = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if path.suffix not in (".py", ".md") or not path.is_file():
            continue
        if path == Path(__file__).resolve():
            continue  # this file quotes the pattern in its own docstring
        rel = path.relative_to(REPO_ROOT)
        # Match on the *relative* parts: the absolute path can itself sit under
        # a skipped name (a git worktree lives in .claude/worktrees/), which
        # would silently skip the entire repo.
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in _STATUS_VOCAB.finditer(text):
            tokens = [t.strip() for t in match.group(1).split("|") if t.strip()]
            if tokens:
                found.append((str(rel), tokens))
    return found


def test_prompt_vocabularies_are_discoverable():
    """Guard the guard: if the prompts get reworded, these tests must not go
    vacuously green. Both the Tier-2 agent prompt and the worker's forensics
    templates declare a STATUS vocabulary today."""
    vocabs = _prompt_vocabularies()
    assert len(vocabs) >= 2, (
        "No STATUS vocabulary found in the prompts — the pattern this suite "
        f"keys on has changed and the checks below would prove nothing: {vocabs}"
    )
    assert any(src.startswith("agent/") for src, _ in vocabs)


@pytest.mark.parametrize("source,tokens", _prompt_vocabularies())
def test_every_prompt_status_is_recognized(source, tokens):
    """Each status a prompt can emit must be *recognized* by normalize_outcome.

    Recognition is the precise property that was missing: an unrecognized token
    does not raise, it silently becomes the default. Asserting only that the
    result is a valid outcome would therefore pass even when the bug is present.
    """
    for token in tokens:
        assert token in VALID_OUTCOMES or token in OUTCOME_ALIASES, (
            f"{source} can emit STATUS '{token}', but it is in neither "
            f"VALID_OUTCOMES nor OUTCOME_ALIASES — normalize_outcome() would "
            f"silently store it as the default outcome"
        )
        assert normalize_outcome(token) in VALID_OUTCOMES


def test_needs_action_survives_normalization():
    """The specific regression, and the two alternatives that were rejected:
    collapsing it into 'monitoring' (today's bug) or aliasing it to 'escalated'
    (wrong semantics — escalated means page a human now, needs_action does not).
    """
    assert normalize_outcome("needs_action") == "needs_action"
    assert normalize_outcome("needs_action") not in ("monitoring", "escalated")


def test_check_constraint_admits_every_valid_outcome():
    """The CHECK constraint and VALID_OUTCOMES are one source of truth.

    Two hand-maintained lists is exactly how needs_action came to be missing
    from both; this pins that they can no longer drift apart.
    """
    constraint = next(
        c for c in Investigation.__table__.constraints
        if getattr(c, "name", None) == "valid_outcome"
    )
    sql = str(constraint.sqltext)
    assert sql == OUTCOME_CHECK_SQL
    for outcome in VALID_OUTCOMES:
        assert f"'{outcome}'" in sql, (
            f"'{outcome}' is a valid outcome the code will try to write, but the "
            f"valid_outcome CHECK rejects it: {sql}"
        )


def test_aliases_resolve_to_valid_outcomes():
    """An alias pointing at a non-outcome would re-open the same hole from the
    other side — recognized, but still unstorable."""
    for alias, target in OUTCOME_ALIASES.items():
        assert target in VALID_OUTCOMES, f"alias {alias!r} -> {target!r} is not storable"
