"""A replayed investigation update carries its completion time (CFOP-433).

The offline branch of ResilientKnowledgeBase.update_investigation used to
buffer everything except ``completed_at``, and the replay passed none, so an
investigation whose final update went through the outbox was stored with a
terminal outcome and ``completed_at NULL`` for good (#3825 under CFOP-430).

Pinned here: the buffered payload carries the stamp as ISO 8601; the replay
passes it back as a datetime; a payload from the older code (no key) with a
terminal outcome falls back to the event's own buffered timestamp; an update
with no outcome, or ``in_progress``, gets no stamp.
"""

import os
import sys
import threading
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from knowledge_base import ResilientKnowledgeBase, _replayed_completed_at
from local_buffer import BufferedEvent


class _StubKB:
    def __init__(self):
        self.updates = []

    def update_investigation(self, **kwargs):
        self.updates.append(kwargs)
        return True


class _OfflineMonitor:
    def is_healthy(self):
        return False


class _StubBuffer:
    def __init__(self):
        self.events = []

    def buffer_event(self, event_type, data):
        self.events.append((event_type, data))
        return len(self.events)


def _rkb():
    rkb = ResilientKnowledgeBase.__new__(ResilientKnowledgeBase)
    rkb._kb = _StubKB()
    rkb._health_monitor = _OfflineMonitor()
    rkb._buffer = _StubBuffer()
    rkb._local_to_db_id_map = {}
    rkb._local_id_lock = threading.Lock()
    rkb._local_inv_id_counter = 0
    return rkb


def _event(data, ts="2026-10-16T10:23:56.223520+00:00"):
    return BufferedEvent(event_type="update_investigation", timestamp=ts,
                         host_id="h", data=data, sequence=3)


# ----------------------------------------------------------------------------
# Buffering keeps the stamp
# ----------------------------------------------------------------------------

def test_offline_update_buffers_completed_at_as_iso():
    rkb = _rkb()
    stamp = datetime(2026, 10, 16, 10, 23, 56, 223520)
    assert rkb.update_investigation(investigation_id=-2, completed_at=stamp,
                                    findings={"a": 1}, outcome="needs_action",
                                    duration_seconds=66.9, tool_calls_count=9) is True
    (kind, data), = rkb._buffer.events
    assert kind == "update_investigation"
    assert data["completed_at"] == "2026-10-16T10:23:56.223520"
    assert data["outcome"] == "needs_action"


def test_offline_update_without_stamp_buffers_none():
    rkb = _rkb()
    rkb.update_investigation(investigation_id=-2, findings={"a": 1}, outcome="monitoring")
    (_, data), = rkb._buffer.events
    assert data["completed_at"] is None


# ----------------------------------------------------------------------------
# Replay passes it through, or falls back to the event time
# ----------------------------------------------------------------------------

def test_replay_passes_the_buffered_stamp():
    rkb = _rkb()
    rkb._local_to_db_id_map[-2] = 3825
    rkb._replay_event(_event({"investigation_id": -2,
                              "completed_at": "2026-10-16T10:23:56.223520",
                              "findings": {"a": 1}, "outcome": "needs_action",
                              "duration_seconds": 66.9, "tool_calls_count": 9}))
    (call,) = rkb._kb.updates
    assert call["investigation_id"] == 3825
    assert call["completed_at"] == datetime(2026, 10, 16, 10, 23, 56, 223520)
    assert call["outcome"] == "needs_action"


def test_replay_of_legacy_payload_uses_the_event_time_for_a_terminal_outcome():
    """An outbox line written before the key existed must still complete the row.

    Mutation check: return None from the fallback branch of
    _replayed_completed_at and this fails.
    """
    rkb = _rkb()
    rkb._local_to_db_id_map[-2] = 3825
    rkb._replay_event(_event({"investigation_id": -2, "findings": {"a": 1},
                              "outcome": "needs_action", "duration_seconds": 1.0,
                              "tool_calls_count": 1}))
    (call,) = rkb._kb.updates
    assert call["completed_at"] == datetime(2026, 10, 16, 10, 23, 56, 223520,
                                            tzinfo=timezone.utc)


def test_replay_without_outcome_or_in_progress_gets_no_stamp():
    assert _replayed_completed_at({"findings": {"a": 1}}, "2026-10-16T10:23:56+00:00") is None
    assert _replayed_completed_at({"outcome": "in_progress"}, "2026-10-16T10:23:56+00:00") is None
    assert _replayed_completed_at({"outcome": "needs_action", "completed_at": None}, None) is None


def test_unparseable_stamp_falls_back_to_the_event_time():
    got = _replayed_completed_at({"outcome": "resolved", "completed_at": "yesterday"},
                                 "2026-10-16T10:23:56+00:00")
    assert got == datetime(2026, 10, 16, 10, 23, 56, tzinfo=timezone.utc)
