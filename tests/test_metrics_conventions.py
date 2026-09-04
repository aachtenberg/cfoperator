"""Conventions every Prometheus metric in this repo has to keep (CFOP-163).

Two of them were broken for weeks without anything noticing:

- `cfoperator_llm_fallbacks_total` was declared with the metric block and never
  incremented, while docs/METRICS.md shipped an alert on it. A metric that is
  declared and never observed is worse than none: it documents a signal that
  does not exist.
- An unlabelled Gauge is exported the moment it is registered, with the value
  0.0, so a `time() - metric > N` age query fires from process start until the
  first set() (HOMELAB-15, CFOP-152). Timestamp-style gauges must be labelled
  so no child series exists until the first real value.

Source-level checks, not scrapes, on purpose: they run in CI with no server.
"""

from repo_paths import REPO_ROOT
import re
from pathlib import Path

_SEARCH_DIRS = ("agent", "event_runtime", "observability", "auth", ".")
# Leading indentation allowed: event_runtime/telemetry.py declares every
# metric inside `if PROMETHEUS_AVAILABLE:` (review of CFOP-163 caught the
# column-zero anchor skipping that whole block).
_DECL = re.compile(
    r"^[ \t]*(?P<name>[A-Z][A-Z0-9_]*)\s*=\s*(?P<kind>Counter|Gauge|Histogram|Summary|Info)\s*\(\s*['\"](?P<metric>[a-z][a-z0-9_]+)['\"]",
    re.M)
_VERBS = ("labels(", "inc(", "observe(", "set(", "info(", "dec(", "set_to_current_time(")
# Metrics observed through a local alias the source check cannot follow. Each
# entry must still be IMPORTED somewhere (checked below) so a truly dead one
# cannot hide here; the reason says where the .labels() call actually is.
_OBSERVED_VIA_ALIAS = {
    "EMBEDDING_CACHE_HITS": "agent/embedding_service.py binds it via _metrics() and calls .labels() on the local",
}


def _sources():
    seen = set()
    for d in _SEARCH_DIRS:
        base = REPO_ROOT / d
        for p in (base.glob("*.py") if d == "." else base.rglob("*.py")):
            if ".venv" in p.parts or "node_modules" in p.parts or p.name.startswith("test_"):
                continue
            if p.resolve() in seen:
                continue
            seen.add(p.resolve())
            yield p, p.read_text(errors="replace")


def _declarations():
    for p, s in _sources():
        for m in _DECL.finditer(s):
            yield p, m.group("name"), m.group("kind"), m.group("metric"), s


def test_every_declared_metric_is_observed_somewhere():
    corpus = {p: s for p, s in _sources()}
    orphans = []
    for p, name, kind, metric, s in _declarations():
        used = False
        for q, body in corpus.items():
            for m in re.finditer(r"\b" + re.escape(name) + r"\.(\w+)\(", body):
                if m.group(1) + "(" in _VERBS:
                    used = True
                    break
            if used:
                break
        if not used and name in _OBSERVED_VIA_ALIAS:
            used = any(name in body and q != p for q, body in corpus.items())
        if not used:
            orphans.append(f"{p.relative_to(REPO_ROOT)}: {name} ({metric})")
    assert not orphans, "declared but never observed:\n  " + "\n  ".join(orphans)


def test_timestamp_gauges_are_labelled():
    bad = []
    for p, s in _sources():
        for m in re.finditer(r"Gauge\s*\((.*?)\)\s*$", s, re.M | re.S):
            call = m.group(1)
            name = re.search(r"['\"]([a-z0-9_]+)['\"]", call)
            if not name or not name.group(1).endswith("_timestamp_seconds"):
                continue
            if "[" not in call:
                bad.append(f"{p.relative_to(REPO_ROOT)}: {name.group(1)}")
    assert not bad, "unlabelled timestamp gauge(s) export 0.0 before the first set():\n  " + "\n  ".join(bad)


def test_console_decision_routes_record_the_human_gate():
    src = (REPO_ROOT / "web_server.py").read_text()
    for decision in ("approve", "reject"):
        assert f"REMEDIATION_HUMAN_DECISIONS.labels(decision='{decision}')" in src, decision
