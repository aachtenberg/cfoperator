"""Dynatrace, as an event runtime plugin rather than a shipped backend.

``docs/infrastructure-config.md`` keeps Dynatrace under "not planned" for the
core: the target user self-hosts, and Dynatrace customers already have Davis.
This package lets cfoperator be plugged into a Dynatrace environment anyway,
without the core learning about it.

Load it with ``CFOP_EVENT_RUNTIME_PLUGINS=integrations.dynatrace``. It then
registers ``DynatraceProblemSource`` (``problems.py``), which turns Davis
problems into alerts, and ``DynatraceEvidenceProvider`` (``evidence.py``),
which hands each such alert's investigation Dynatrace's own view of it. Both
read Grail through ``grail.py``. With a write token it also registers
``DynatraceProblemCommenter`` (``writeback.py``), which comments each
investigation's conclusion onto the problem.

Environment:

- ``DT_ENVIRONMENT_URL`` (required): the platform URL,
  ``https://<env>.apps.dynatrace.com``.
- ``DT_PLATFORM_TOKEN`` (required): a platform token that can read
  ``dt.davis.problems`` (``storage:events:read``).
- ``CFOP_DYNATRACE_POLL_SECONDS`` (default 60): the least time between queries.
- ``CFOP_DYNATRACE_LOOKBACK`` (default ``7d``): how far back the problem
  query reaches, as ``<n>m``, ``<n>h`` or ``<n>d``.
- ``CFOP_DYNATRACE_PROBLEM_FILTER`` (optional): a DQL condition scoping which
  problems become alerts, e.g. ``in("my-cluster", k8s.cluster.name)``. Select
  on where a problem is, never on its state: the resolving CLOSED row must
  pass the filter too.
- ``CFOP_DYNATRACE_EVIDENCE`` (default on): ``0``, ``false`` or ``off`` keeps
  the problem source and drops the evidence queries.
- ``DT_PROBLEMS_TOKEN`` (optional): a classic access token (``dt0c01...``) with
  ``problems.write``. Set, each investigation is written back as a comment on
  its problem; unset, nothing is written.
- ``DT_API_URL`` (optional): the classic environment API. Derived from
  ``DT_ENVIRONMENT_URL`` for SaaS (``.apps.`` becomes ``.live.``); set it for
  anything else.

A missing or malformed setting stops the runtime at startup, since the operator
asked for this plugin. Dynatrace being unreachable does not: that is logged
and retried with backoff, and the runtime's other sources carry on.
"""

from __future__ import annotations

import math
import os
from typing import Any


def register(plugins: Any, context: Any) -> None:
    """Entry point for ``CFOP_EVENT_RUNTIME_PLUGINS`` (see event_runtime/external_plugins.py)."""
    from .evidence import DynatraceEvidenceProvider
    from .grail import GrailClient
    from .problems import DynatraceProblemSource
    from .writeback import DynatraceProblemCommenter, classic_api_url

    url = os.getenv("DT_ENVIRONMENT_URL", "").strip()
    token = os.getenv("DT_PLATFORM_TOKEN", "").strip()
    missing = [name for name, value in (("DT_ENVIRONMENT_URL", url), ("DT_PLATFORM_TOKEN", token)) if not value]
    if missing:
        raise ValueError(f"the Dynatrace plugin needs {' and '.join(missing)}")

    raw_poll = os.getenv("CFOP_DYNATRACE_POLL_SECONDS", "60").strip()
    try:
        poll_seconds = float(raw_poll)
    except ValueError:
        poll_seconds = 0.0
    # float() takes "nan" and "inf", and nan < 10 is False: without the
    # isfinite check a NaN would disable the throttle and inf stop polling.
    if not math.isfinite(poll_seconds) or poll_seconds < 10:
        raise ValueError(f"CFOP_DYNATRACE_POLL_SECONDS must be a number of seconds >= 10, got {raw_poll!r}")

    lookback = os.getenv("CFOP_DYNATRACE_LOOKBACK", "7d").strip() or "7d"
    plugins.register_alert_source(
        DynatraceProblemSource(
            GrailClient(url, token),
            escalation_ledger=context.escalation_ledger,
            poll_seconds=poll_seconds,
            lookback=lookback,
            problem_filter=os.getenv("CFOP_DYNATRACE_PROBLEM_FILTER", "").strip() or None,
        )
    )
    if os.getenv("CFOP_DYNATRACE_EVIDENCE", "1").strip().lower() not in ("0", "false", "off", "no"):
        # Its own client: evidence runs inline before triage, so each query
        # gets a shorter timeout than the problem poll does.
        plugins.register_context_provider(
            DynatraceEvidenceProvider(GrailClient(url, token, timeout=10), lookback=lookback)
        )
    problems_token = os.getenv("DT_PROBLEMS_TOKEN", "").strip()
    if problems_token:
        api_url = os.getenv("DT_API_URL", "").strip() or classic_api_url(url)
        plugins.register_completion_observer(DynatraceProblemCommenter(api_url, problems_token))
