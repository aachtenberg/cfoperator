"""DQL evidence for Dynatrace alerts (CFOP-205).

For an alert from ``DynatraceProblemSource`` this runs a few fixed queries and
hands the results to the investigation as evidence (``cfshared/evidence.py``,
CFOP-211). The model reads what Dynatrace saw; it never writes DQL.

Every query was run against a live tenant before it was written here:

- the problem's Davis events: ``dt.davis.event_ids`` on the problem row, then
  ``dt.davis.events`` filtered with ``in(event.id, array(...))``;
- logs for the last hour, grouped by content with a count, so a crash loop
  collapses to one line ("cfop-201: deliberate crash for Davis" x5). For a
  workload, its own logs; for a host, only levels above INFO, because a host's
  logs include every container on it and the informational ones drown the rest
  (723 identical overlayfs lines in one hour on the dev node);
- container restarts (``dt.kubernetes.container.restarts`` by workload), or for
  a host problem its CPU (``dt.host.cpu.usage`` by ``host.name``).

``provide()`` runs inline, before triage, so the queries share a time budget.
A failed or skipped query leaves a visible note, and an empty result says
"(none)", so the investigation can tell missing evidence from absent trouble.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Sequence

from cfshared.evidence import EVIDENCE_KEY
from event_runtime.models import Alert, ContextEnvelope
from event_runtime.plugins import ContextProvider

from .grail import GrailClient, GrailQueryError

logger = logging.getLogger(__name__)

EVIDENCE_NAME = "dynatrace"
_TEXT_LIMIT = 3500          # under cfshared.evidence.BLOCK_LIMIT, so it arrives whole
_LINE_LIMIT = 300
_MAX_EVENT_IDS = 20
_QUIET_LEVELS = ("INFO", "DEBUG", "TRACE", "NONE")


def dql_string(value: Any) -> str:
    """A DQL string literal. Values come from Dynatrace itself, and are escaped anyway."""
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


class DynatraceEvidenceProvider(ContextProvider):
    """Attach Dynatrace's own view of the problem to the investigation."""

    name = "dynatrace-evidence"

    def __init__(
        self,
        client: GrailClient,
        *,
        budget_seconds: float = 20.0,
        lookback: str = "7d",
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._client = client
        self._budget = float(budget_seconds)
        self._lookback = lookback
        self._clock = clock
        self._now = now

    def provide(self, alert: Alert, envelope: ContextEnvelope) -> ContextEnvelope:
        if alert.source != "dynatrace" or alert.details.get("resolution"):
            return envelope
        dynatrace = alert.details.get("dynatrace") or {}
        problem_id = dynatrace.get("problem_id")
        if not problem_id:
            return envelope
        text = self._gather(alert, str(problem_id), str(dynatrace.get("display_id") or problem_id))
        evidence = envelope.context.setdefault(EVIDENCE_KEY, {})
        if isinstance(evidence, dict):
            evidence[EVIDENCE_NAME] = text
        return envelope

    def _gather(self, alert: Alert, problem_id: str, display_id: str) -> str:
        deadline = self._clock() + self._budget
        labels = alert.details.get("labels") or {}
        namespace, workload, host = labels.get("namespace"), labels.get("workload"), labels.get("host")

        sections = [
            f"Dynatrace's view of {display_id}, queried {self._now():%Y-%m-%d %H:%M} UTC. Times are UTC.",
            self._section("Davis events in this problem, newest first", deadline, self._davis_events, problem_id),
        ]
        if namespace and workload:
            where = f"{namespace}/{workload}"
            sections.append(self._section(f"Logs from {where}, last hour, distinct lines, newest first",
                                          deadline, self._workload_logs, namespace, workload))
            sections.append(self._section(f"Container restarts for {where}, last 2 hours",
                                          deadline, self._restarts, namespace, workload))
        elif host:
            sections.append(self._section(f"Logs above INFO from host {host}, last hour, distinct lines, newest first",
                                          deadline, self._host_logs, host))
            sections.append(self._section(f"CPU on host {host}, last 2 hours", deadline, self._host_cpu, host))
        text = "\n\n".join(sections)
        return text if len(text) <= _TEXT_LIMIT else text[: _TEXT_LIMIT - 15] + "\n[... trimmed]"

    def _section(self, title: str, deadline: float, fetch: Callable[..., List[str]], *args: Any) -> str:
        if self._clock() >= deadline:
            return f"{title}:\n(skipped: the {self._budget:g}s evidence budget was used up)"
        try:
            lines = fetch(*args)
        except GrailQueryError as exc:
            logger.warning("Dynatrace evidence query failed (%s): %s", title, exc)
            return f"{title}:\n(query failed: {exc})"
        return f"{title}:\n" + ("\n".join(lines) if lines else "(none)")

    # --- the queries -----------------------------------------------------------

    def _davis_events(self, problem_id: str) -> List[str]:
        rows = self._client.query(
            f"fetch dt.davis.problems, from:-{self._lookback}\n"
            f"| filter event.id == {dql_string(problem_id)}\n"
            "| fields dt.davis.event_ids\n| limit 1"
        ).records
        ids = list((rows[0].get("dt.davis.event_ids") or []) if rows else [])[:_MAX_EVENT_IDS]
        if not ids:
            return []
        events = self._client.query(
            f"fetch dt.davis.events, from:-{self._lookback}\n"
            f"| filter in(event.id, array({', '.join(dql_string(i) for i in ids)}))\n"
            "| sort timestamp desc\n"
            "| fields timestamp, event.status, event.name, event.description\n| limit 10"
        ).records
        return [
            _line(f"- {_clock_time(e.get('timestamp'))} {e.get('event.status') or '?'} {e.get('event.name') or '?'}"
                  + (f": {e['event.description']}" if e.get("event.description") else ""))
            for e in events
        ]

    def _workload_logs(self, namespace: str, workload: str) -> List[str]:
        return self._grouped_logs(
            f"k8s.namespace.name == {dql_string(namespace)} and k8s.workload.name == {dql_string(workload)}"
        )

    def _host_logs(self, host: str) -> List[str]:
        quiet = ", ".join(dql_string(level) for level in _QUIET_LEVELS)
        return self._grouped_logs(f"host.name == {dql_string(host)} and not in(loglevel, array({quiet}))")

    def _grouped_logs(self, condition: str) -> List[str]:
        rows = self._client.query(
            "fetch logs, from:-1h\n"
            f"| filter {condition}\n"
            "| summarize n = count(), last = max(timestamp), by:{content, loglevel}\n"
            "| sort last desc\n| limit 10"
        ).records
        lines = []
        for row in rows:
            level = str(row.get("loglevel") or "")
            shown_level = f"[{level}] " if level and level != "NONE" else ""
            content = " ".join(str(row.get("content") or "").split())
            lines.append(_line(f"- x{row.get('n')}, last {_clock_time(row.get('last'))} {shown_level}{content}"))
        return lines

    def _restarts(self, namespace: str, workload: str) -> List[str]:
        rows = self._client.query(
            "timeseries r = sum(dt.kubernetes.container.restarts), by:{k8s.workload.name}, "
            f"filter: k8s.namespace.name == {dql_string(namespace)} and k8s.workload.name == {dql_string(workload)}, "
            "from:-2h, interval:10m\n"
            "| fieldsAdd total = arraySum(r)"
        ).records
        if not rows:
            return []
        row = rows[0]
        return [f"- {_number(row.get('total'))} in total; per 10 minutes, oldest first: {_series(row.get('r'))}"]

    def _host_cpu(self, host: str) -> List[str]:
        rows = self._client.query(
            f"timeseries cpu = avg(dt.host.cpu.usage), filter: host.name == {dql_string(host)}, "
            "from:-2h, interval:10m\n"
            "| fieldsAdd peak = arrayMax(cpu), last = arrayLast(cpu)"
        ).records
        if not rows:
            return []
        row = rows[0]
        return [f"- CPU %: peak {_number(row.get('peak'))}, latest {_number(row.get('last'))}; "
                f"per 10 minutes, oldest first: {_series(row.get('cpu'))}"]


def _clock_time(raw: Any) -> str:
    text = str(raw or "")
    return text[11:19] if len(text) >= 19 else "?"


def _number(value: Any) -> str:
    if value is None:
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else f"{number:.1f}"


def _series(values: Sequence[Any] | None) -> str:
    return " ".join(_number(v) for v in (values or [])) or "-"


def _line(text: str) -> str:
    return text if len(text) <= _LINE_LIMIT else text[: _LINE_LIMIT - 3] + "..."
