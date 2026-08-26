#!/usr/bin/env python3
"""docs/REMEDIATION.md's FIX contract section must match the code (CFOP-99).

The section exists because a docs sweep found the FIX contract — the object
that decides what becomes a remediation at all — documented nowhere, while
five changes had landed on it (CFOP-70/71/78/85/88). REMEDIATION.md described
a classifier-driven pipeline that had been substantially replaced.

Prose drifts silently; a table does not have to. These guard the two parts of
the section that are mechanically checkable against the source of truth, so
adding a target kind or a schema field without touching the doc goes red
rather than quietly making the doc wrong again.

What is deliberately NOT guarded: the prose. A test that pinned wording would
fail on every edit and teach people to weaken it. The invalid-FIX list, the
confidence rules and the judge behaviour are covered by
agent/test_structured_fix.py against the code itself.
"""

import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
# agent/agent.py imports its siblings bare (``knowledge_base``), so the package
# directory has to be on the path as well as the repo root -- the same
# arrangement .github/workflows/tests.yml gives the agent suite.
# Root FIRST: with agent/ ahead of it, ``agent`` resolves to agent/agent.py
# instead of the package and the import fails.
sys.path.insert(0, _ROOT)
sys.path.append(os.path.join(_ROOT, 'agent'))

from agent.agent import _FIX_JSON_SCHEMA, _FIX_KIND_TO_CLASS  # noqa: E402

DOC = os.path.join(_ROOT, 'docs', 'REMEDIATION.md')


def _doc_text():
    with open(DOC, encoding='utf-8') as fh:
        return fh.read()


def _documented_kind_class_pairs(text):
    """Rows of the `| target kind | remediation class |` table, as a dict."""
    section = text.split('### What a valid FIX decides', 1)
    assert len(section) == 2, "the 'What a valid FIX decides' heading is gone"
    pairs = {}
    for line in section[1].splitlines():
        row = re.match(r'^\|\s*`([a-z0-9-]+)`\s*\|\s*`([a-z0-9-]+)`\s*\|\s*$', line)
        if row:
            pairs[row.group(1)] = row.group(2)
        elif pairs and not line.startswith('|'):
            break          # table ended
    return pairs


def test_the_documented_kind_table_matches_the_code():
    """Add a target kind without documenting it and this fails.

    That is the exact drift CFOP-99 found: the map grew, the doc did not.
    """
    documented = _documented_kind_class_pairs(_doc_text())
    assert documented, "no kind/class table found in the FIX contract section"
    assert documented == _FIX_KIND_TO_CLASS, (
        f"doc table {documented} != _FIX_KIND_TO_CLASS {_FIX_KIND_TO_CLASS}")


def test_every_schema_field_is_documented():
    """The prompt's own field list is the contract a reader needs.

    _FIX_JSON_SCHEMA is valid JSON (every value is a string), so the field
    names come from parsing it rather than from a second hand-kept list.
    """
    schema = json.loads(_FIX_JSON_SCHEMA)
    text = _doc_text().split('## The FIX contract', 1)[1]
    for field in schema:
        assert f'"{field}"' in text, f"schema field {field!r} is undocumented"


def test_nested_target_and_observed_keys_are_documented():
    """`observed` being required is the load-bearing half of CFOP-88, and its
    two keys are what make an entry valid; a doc naming the field but not its
    shape would not let a reader tell a valid FIX from an invalid one."""
    schema = json.loads(_FIX_JSON_SCHEMA)
    text = _doc_text().split('## The FIX contract', 1)[1]
    for parent in ('targets', 'observed'):
        for key in schema[parent][0]:
            assert f'"{key}"' in text, f"{parent}.{key} is undocumented"
