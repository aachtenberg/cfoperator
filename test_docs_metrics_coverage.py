#!/usr/bin/env python3
"""Every Prometheus metric declared in the tree must appear in METRICS.md (CFOP-99).

A docs sweep found 20 declared metrics absent from the reference, including the
counters behind features shipped that fortnight — the judge and node-fold gates
(CFOP-70/71). The observability existed and nobody could find it.

Metric names are mechanically checkable, so this is a guard rather than a
one-time correction: adding a Counter without documenting it now goes red.

Two things this deliberately does NOT do:

* It does not scrape a live endpoint. `prometheus_client` emits nothing for a
  LABELLED metric until some label combination is observed, so a running pod
  shows only what has fired — during the sweep that made 14 correctly
  documented metrics look absent. The declaration is the truth here.
* It does not check help text, labels or example queries. Those drift for good
  reasons and pinning them would make the test noise.
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

DOC = os.path.join(_ROOT, 'docs', 'METRICS.md')

#: Trees whose metrics are exposed by a process this doc covers. The ephemeral
#: executor/worker Jobs are excluded on purpose — they scrape nowhere.
_SEARCH_DIRS = ('agent', 'event_runtime', 'observability', 'auth',
                'tools', 'mcp_server', 'bridge', 'discovery')

#: prometheus_client renders a counter as `_total` and a histogram as
#: `_sum`/`_count`/`_bucket`; compare on the stem so a doc naming either form
#: counts as documenting the metric.
_SUFFIX = re.compile(r'_(total|created|sum|count|bucket)$')

_INFO = re.compile(r'\bInfo\s*\(\s*["\']([a-z][a-z0-9_]+)["\']', re.S)
_OTHER = re.compile(
    r'\b(?:Counter|Gauge|Histogram|Summary)\s*\(\s*["\']([a-z][a-z0-9_]+)["\']', re.S)


def _stem(name):
    prev = None
    while prev != name:
        prev, name = name, _SUFFIX.sub('', name)
    return name


def _declared():
    names = set()
    for d in _SEARCH_DIRS:
        root = os.path.join(_ROOT, d)
        if not os.path.isdir(root):
            continue
        for dirpath, _, files in os.walk(root):
            if '__pycache__' in dirpath:
                continue
            for f in files:
                if not f.endswith('.py'):
                    continue
                with open(os.path.join(dirpath, f), encoding='utf-8',
                          errors='ignore') as fh:
                    text = fh.read()
                names |= set(_OTHER.findall(text))
                # Info('x') is exposed as x_info.
                names |= {n + '_info' for n in _INFO.findall(text)}
    return {_stem(n) for n in names}


def _documented():
    with open(DOC, encoding='utf-8') as fh:
        text = fh.read()
    return {_stem(m) for m in
            re.findall(r'\b(?:cfoperator|log)_[a-z0-9_]+\b', text)}


def test_every_declared_metric_is_documented():
    """Declare a Counter, forget METRICS.md, and this fails."""
    declared, documented = _declared(), _documented()
    assert declared, "found no metric declarations — the parser is broken"
    missing = sorted(declared - documented)
    assert not missing, (
        f"{len(missing)} metric(s) declared but absent from docs/METRICS.md: "
        f"{missing}")


def test_the_parser_actually_finds_the_known_metrics():
    """Guard against the guard passing because it found nothing.

    Every failed cross-check during the sweep came from a broken extractor
    rather than a real gap — a regex that missed names on the line after
    `Counter(`, one that matched session cookies. An empty `declared` set would
    make the test above pass silently, so pin a few names that must be there.
    """
    declared = _declared()
    for name in ('cfoperator_remediation_judge', 'cfoperator_llm_requests',
                 'cfoperator_event_runtime_alerts_received'):
        assert name in declared, f"parser did not find {name}"
