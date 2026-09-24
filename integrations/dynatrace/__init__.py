"""Dynatrace, as an event runtime plugin rather than a shipped backend.

``docs/infrastructure-config.md`` keeps Dynatrace under "not planned" for the
core: the target user self-hosts, and Dynatrace customers already have Davis.
This package lets cfoperator be plugged into a Dynatrace environment anyway,
without the core learning about it.

Load it with ``CFOP_EVENT_RUNTIME_PLUGINS=integrations.dynatrace``. It then
registers ``DynatraceProblemSource`` (``problems.py``), which turns Davis
problems into alerts, reading Grail through ``grail.py``.

Environment:

- ``DT_ENVIRONMENT_URL`` (required): the platform URL,
  ``https://<env>.apps.dynatrace.com``.
- ``DT_PLATFORM_TOKEN`` (required): a platform token that can read
  ``dt.davis.problems`` (``storage:events:read``).
- ``CFOP_DYNATRACE_POLL_SECONDS`` (default 60): the least time between queries.
- ``CFOP_DYNATRACE_LOOKBACK`` (default ``7d``): how far back the problem
  query reaches, as ``<n>m``, ``<n>h`` or ``<n>d``.

A missing or malformed setting stops the runtime at startup, since the operator
asked for this plugin. Dynatrace being unreachable does not: that is logged
and retried with backoff, and the runtime's other sources carry on.
"""

from __future__ import annotations

import os
from typing import Any


def register(plugins: Any, context: Any) -> None:
    """Entry point for ``CFOP_EVENT_RUNTIME_PLUGINS`` (see event_runtime/external_plugins.py)."""
    from .grail import GrailClient
    from .problems import DynatraceProblemSource

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
    if poll_seconds < 10:
        raise ValueError(f"CFOP_DYNATRACE_POLL_SECONDS must be a number of seconds >= 10, got {raw_poll!r}")

    plugins.register_alert_source(
        DynatraceProblemSource(
            GrailClient(url, token),
            escalation_ledger=context.escalation_ledger,
            poll_seconds=poll_seconds,
            lookback=os.getenv("CFOP_DYNATRACE_LOOKBACK", "7d").strip() or "7d",
        )
    )
