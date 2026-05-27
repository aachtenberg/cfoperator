"""Tests for the OODA-loop heartbeat helpers.

The heartbeat line is the only signal that the agent is alive between events
when ``ooda.reactive_poll: false`` and no HTTP investigations are flowing.
"""

from __future__ import annotations

import os
import queue
import sys
import threading as _t
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator


def _operator(*, reactive_poll: bool = False, last_sweep: float | None = None,
              queue_items: int = 0, uptime_seconds: float = 0.0) -> CFOperator:
    """Build a minimal CFOperator with just the state _format_heartbeat reads."""
    op = CFOperator.__new__(CFOperator)
    op.config = {}
    op._reactive_poll_enabled = reactive_poll
    op.start_time = time.time() - uptime_seconds
    op.last_sweep = last_sweep if last_sweep is not None else 0
    op.last_heartbeat = op.start_time
    op._investigation_queue = queue.Queue(maxsize=32)
    op._investigation_lock = _t.Lock()
    for _ in range(queue_items):
        op._investigation_queue.put_nowait({"summary": "x"})
    return op


# ---- _format_heartbeat ----------------------------------------------------


def test_heartbeat_includes_grep_target_and_key_fields():
    op = _operator(reactive_poll=False, uptime_seconds=125, queue_items=3)
    op.last_sweep = time.time() - 600  # 10 minutes ago
    msg = op._format_heartbeat()
    assert msg.startswith("OODA heartbeat:"), "single grep target so logs can be filtered"
    assert "uptime=" in msg
    assert "queue_depth=3" in msg
    assert "last_sweep=" in msg
    assert "10m ago" in msg
    assert "reactive_poll=off" in msg


def test_heartbeat_says_never_when_no_sweep_yet():
    op = _operator(last_sweep=0)
    assert "last_sweep=never" in op._format_heartbeat()


def test_heartbeat_reflects_reactive_poll_on():
    op = _operator(reactive_poll=True)
    assert "reactive_poll=on" in op._format_heartbeat()


def test_heartbeat_queue_depth_starts_at_zero():
    op = _operator(queue_items=0)
    assert "queue_depth=0" in op._format_heartbeat()


# ---- _get_heartbeat_interval ---------------------------------------------


def test_heartbeat_interval_defaults_to_300():
    op = CFOperator.__new__(CFOperator)
    op.config = {}
    # No kb attached → exception path runs → default kicks in.
    op.kb = None  # so getattr/access on get_setting raises and we hit the except
    assert op._get_heartbeat_interval() == 300


def test_heartbeat_interval_reads_config():
    op = CFOperator.__new__(CFOperator)
    op.config = {'ooda': {'heartbeat_interval_seconds': 120}}
    op.kb = None
    assert op._get_heartbeat_interval() == 120


def test_heartbeat_interval_clamps_low_values():
    op = CFOperator.__new__(CFOperator)
    op.config = {}
    # Simulate a DB setting; the clamp keeps the loop from log-spamming.
    class _KB:
        def get_setting(self, key, default):
            return "5"  # below the 30s floor
    op.kb = _KB()
    assert op._get_heartbeat_interval() == 30


def test_heartbeat_interval_clamps_high_values():
    op = CFOperator.__new__(CFOperator)
    op.config = {}
    class _KB:
        def get_setting(self, key, default):
            return "999999"
    op.kb = _KB()
    assert op._get_heartbeat_interval() == 3600


def test_heartbeat_interval_db_setting_wins_over_config():
    op = CFOperator.__new__(CFOperator)
    op.config = {'ooda': {'heartbeat_interval_seconds': 120}}
    class _KB:
        def get_setting(self, key, default):
            return "600"
    op.kb = _KB()
    assert op._get_heartbeat_interval() == 600
