"""Outbound liveness ping to a dead-man's-switch endpoint (CFOP-152).

Why this exists at all, given ``cfoperator_event_runtime_last_poll_timestamp_seconds``
already says when the runtime last worked: that metric is only useful while
Prometheus is scraping it and Alertmanager can deliver the result. Both live in
the same cluster as the runtime, so a failure that takes out the cluster takes
out the evidence with it. A push to something OUTSIDE observes the runtime from
a place the runtime cannot break.

The direction matters. This is a *push*, and the endpoint alerts on **silence** —
that is what makes it a dead-man's switch rather than one more thing that has to
be scraped. Nothing here decides when to alert; it only says "still here".

Deliberately not a NotificationSink. Sinks carry incidents to humans and are
rendered, deduped and severity-routed; a heartbeat is a bare fact on a timer,
and giving it a severity would eventually put it in someone's phone.

No vendor is baked in. The URL is whatever the operator runs — healthchecks.io,
an Uptime Kuma push monitor, a cron-ping on a VPS. Unconfigured means silent,
never a guess (CFOP-148), and an absent URL stays distinguishable from a chosen
one rather than being filled in by a schema default (CFOP-154).
"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request
from typing import Optional

_log = logging.getLogger(__name__)

_ALLOWED_METHODS = ("GET", "POST")


class HeartbeatPusher:
    """Pings ``url`` on a minimum interval. Inert when no URL is configured.

    Every value is injected; nothing about the destination is decided here.
    ``beat()`` never raises — a heartbeat that could break the poll loop it
    reports on would be worse than no heartbeat, because the failure it caused
    would look exactly like the failure it exists to detect.
    """

    def __init__(
        self,
        url: str = "",
        *,
        method: str = "GET",
        timeout_seconds: int = 5,
        min_interval_seconds: int = 0,
    ):
        self.url = (url or "").strip()
        m = (method or "GET").strip().upper()
        # An unrecognised method is corrected rather than refused: the ping is
        # a diagnostic, and dropping liveness reporting over a typo in a verb
        # would be a worse outcome than sending the wrong-but-working one.
        if m not in _ALLOWED_METHODS:
            if self.url:
                _log.warning(
                    "heartbeat.method %r is not one of %s; using GET",
                    method, "/".join(_ALLOWED_METHODS),
                )
            m = "GET"
        self.method = m
        self.timeout_seconds = max(1, int(timeout_seconds or 5))
        self.min_interval_seconds = max(0, int(min_interval_seconds or 0))
        self._last_sent: Optional[float] = None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def due(self, now: Optional[float] = None) -> bool:
        """Has min_interval_seconds elapsed since the last successful send?"""
        if not self.enabled:
            return False
        if self._last_sent is None:
            return True
        now = time.time() if now is None else now
        return (now - self._last_sent) >= self.min_interval_seconds

    def beat(self, now: Optional[float] = None) -> bool:
        """Send one ping. Returns True only when the endpoint accepted it.

        A False here is not an incident on its own -- the endpoint is the thing
        that decides, by noticing the silence.
        """
        if not self.enabled or not self.due(now):
            return False
        req = urllib.request.Request(self.url, method=self.method)
        if self.method == "POST":
            # Some receivers reject a POST with no body.
            req.data = b""
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                ok = 200 <= getattr(resp, "status", 0) < 300
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            # Warning, not exception: this fires on every poll while an endpoint
            # is unreachable, and a stack trace per cycle would bury the log the
            # operator is reading to diagnose the outage.
            _log.warning("Heartbeat push failed (%s): %s", self.method, exc)
            return False
        if ok:
            self._last_sent = time.time() if now is None else now
        else:
            _log.warning("Heartbeat push returned a non-2xx status")
        return ok


def build_heartbeat_pusher(config: dict | None) -> HeartbeatPusher:
    """Construct from ``event_runtime.heartbeat``; unconfigured -> inert.

    Pure, so the config contract is testable without starting a runtime.
    """
    block = {}
    if isinstance(config, dict):
        er = config.get("event_runtime")
        if isinstance(er, dict):
            hb = er.get("heartbeat")
            if isinstance(hb, dict):
                block = hb
    return HeartbeatPusher(
        str(block.get("url") or ""),
        method=str(block.get("method") or "GET"),
        timeout_seconds=int(block.get("timeout_seconds") or 5),
        min_interval_seconds=int(block.get("min_interval_seconds") or 0),
    )
