"""LocalEventBuffer: the offline path that must not lose or reorder events.

Everything here runs against a tmp_path buffer dir — no /data/buffer, no
PostgreSQL. The invariants under test are the ones replay depends on:
monotonic sequence numbers (across restarts too), sequence-ordered reads,
size-capped retention, and cleanup that never drops the file still being
written.
"""

import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from local_buffer import BufferedEvent, LocalEventBuffer


@pytest.fixture
def buf(tmp_path):
    b = LocalEventBuffer(host_id="testhost", buffer_dir=str(tmp_path / "buffer"))
    yield b
    b.close()


class TestBufferEvent:
    def test_creates_buffer_dir(self, tmp_path):
        target = tmp_path / "nested" / "buffer"
        b = LocalEventBuffer(host_id="h1", buffer_dir=str(target))
        try:
            assert target.is_dir()
        finally:
            b.close()

    def test_sequence_starts_at_one_and_increments(self, buf):
        assert buf.buffer_event("start_investigation", {"alert": "a"}) == 1
        assert buf.buffer_event("investigation_event", {"step": 1}) == 2
        assert buf.buffer_event("update_investigation", {"status": "done"}) == 3

    def test_event_round_trips_with_payload(self, buf):
        buf.buffer_event("start_investigation", {"alert_id": "abc", "nested": {"k": [1, 2]}})

        events = buf.get_pending_events()
        assert len(events) == 1
        event = events[0]
        assert isinstance(event, BufferedEvent)
        assert event.event_type == "start_investigation"
        assert event.host_id == "testhost"
        assert event.sequence == 1
        assert event.data == {"alert_id": "abc", "nested": {"k": [1, 2]}}
        assert event.timestamp  # ISO stamp, set at buffer time

    def test_written_as_json_lines(self, buf, tmp_path):
        buf.buffer_event("a", {"i": 1})
        buf.buffer_event("b", {"i": 2})

        files = list((tmp_path / "buffer").glob("events_testhost_*.jsonl"))
        assert len(files) == 1
        lines = [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]
        assert [line["event_type"] for line in lines] == ["a", "b"]

    def test_non_serializable_payload_falls_back_to_str(self, buf):
        buf.buffer_event("weird", {"obj": object()})

        assert buf.get_pending_events()[0].data["obj"].startswith("<object object")

    def test_concurrent_writers_get_unique_sequences(self, buf):
        seqs = []
        lock = threading.Lock()

        def write():
            for _ in range(20):
                seq = buf.buffer_event("investigation_event", {})
                with lock:
                    seqs.append(seq)

        threads = [threading.Thread(target=write) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(seqs) == list(range(1, 81))


class TestSequenceRecovery:
    def test_resumes_sequence_from_existing_files(self, tmp_path):
        first = LocalEventBuffer(host_id="testhost", buffer_dir=str(tmp_path / "buffer"))
        first.buffer_event("a", {})
        first.buffer_event("b", {})
        first.close()

        second = LocalEventBuffer(host_id="testhost", buffer_dir=str(tmp_path / "buffer"))
        try:
            assert second.buffer_event("c", {}) == 3
        finally:
            second.close()

    def test_ignores_other_hosts_sequences(self, tmp_path):
        other = LocalEventBuffer(host_id="otherhost", buffer_dir=str(tmp_path / "buffer"))
        for _ in range(5):
            other.buffer_event("a", {})
        other.close()

        mine = LocalEventBuffer(host_id="testhost", buffer_dir=str(tmp_path / "buffer"))
        try:
            assert mine.buffer_event("a", {}) == 1
        finally:
            mine.close()

    def test_corrupt_file_does_not_block_init(self, tmp_path):
        buffer_dir = tmp_path / "buffer"
        buffer_dir.mkdir(parents=True)
        (buffer_dir / "events_testhost_20240101_000000_000000.jsonl").write_text(
            '{"sequence": 7, "event_type": "a", "timestamp": "t", "host_id": "testhost", "data": {}}\n'
            "not json at all\n"
        )

        b = LocalEventBuffer(host_id="testhost", buffer_dir=str(buffer_dir))
        try:
            # The truncated line aborts the rest of that file's scan, but the
            # sequences read before it still count — no restart from 1.
            assert b.buffer_event("a", {}) == 8
        finally:
            b.close()


class TestGetPendingEvents:
    def test_empty_buffer(self, buf):
        assert buf.get_pending_events() == []
        assert buf.pending_count() == 0
        assert buf.has_pending_events() is False

    def test_sorted_by_sequence_across_files(self, tmp_path):
        buffer_dir = tmp_path / "buffer"
        buffer_dir.mkdir(parents=True)

        def write(name, sequences):
            lines = [
                json.dumps({
                    "event_type": "e",
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "host_id": "testhost",
                    "data": {},
                    "sequence": seq,
                })
                for seq in sequences
            ]
            (buffer_dir / name).write_text("\n".join(lines) + "\n")

        # Later filename, lower sequences — filename order must not win.
        write("events_testhost_20240102_000000_000000.jsonl", [1, 2])
        write("events_testhost_20240101_000000_000000.jsonl", [3, 4])

        b = LocalEventBuffer(host_id="testhost", buffer_dir=str(buffer_dir))
        try:
            assert [e.sequence for e in b.get_pending_events()] == [1, 2, 3, 4]
        finally:
            b.close()

    def test_unreadable_file_is_skipped(self, buf, tmp_path):
        buf.buffer_event("good", {})
        (tmp_path / "buffer" / "events_testhost_bogus.jsonl").write_text("{oops\n")

        assert [e.event_type for e in buf.get_pending_events()] == ["good"]

    def test_blank_lines_ignored_by_count(self, buf, tmp_path):
        buf.buffer_event("a", {})
        current = next((tmp_path / "buffer").glob("events_testhost_*.jsonl"))
        with open(current, "a") as f:
            f.write("\n   \n")

        assert buf.pending_count() == 1
        assert buf.has_pending_events() is True


class TestMarkSynced:
    def test_deletes_fully_synced_files_but_keeps_current(self, tmp_path):
        buffer_dir = tmp_path / "buffer"
        first = LocalEventBuffer(host_id="testhost", buffer_dir=str(buffer_dir))
        first.buffer_event("a", {})
        first.buffer_event("b", {})
        first.close()

        second = LocalEventBuffer(host_id="testhost", buffer_dir=str(buffer_dir))
        try:
            second.buffer_event("c", {})  # sequence 3, in the current file
            second.mark_synced(3)

            remaining = list(buffer_dir.glob("events_testhost_*.jsonl"))
            assert remaining == [second._current_file]
            assert [e.sequence for e in second.get_pending_events()] == [3]
        finally:
            second.close()

    def test_keeps_file_with_unsynced_events(self, tmp_path):
        buffer_dir = tmp_path / "buffer"
        first = LocalEventBuffer(host_id="testhost", buffer_dir=str(buffer_dir))
        first.buffer_event("a", {})
        first.buffer_event("b", {})
        first.close()

        second = LocalEventBuffer(host_id="testhost", buffer_dir=str(buffer_dir))
        try:
            second.mark_synced(1)  # file's max sequence is 2 — not fully synced
            assert [e.sequence for e in second.get_pending_events()] == [1, 2]
        finally:
            second.close()

    def test_corrupt_file_survives_cleanup(self, tmp_path):
        buffer_dir = tmp_path / "buffer"
        buffer_dir.mkdir(parents=True)
        corrupt = buffer_dir / "events_testhost_20240101_000000_000000.jsonl"
        corrupt.write_text("{not json\n")

        b = LocalEventBuffer(host_id="testhost", buffer_dir=str(buffer_dir))
        try:
            b.mark_synced(999)
            assert corrupt.exists()
        finally:
            b.close()


class TestRotationAndRetention:
    def test_rotates_when_file_exceeds_max_size(self, tmp_path):
        b = LocalEventBuffer(
            host_id="testhost",
            buffer_dir=str(tmp_path / "buffer"),
            max_file_size_mb=0,  # every write trips the rotation check
        )
        try:
            b.buffer_event("a", {})
            b.buffer_event("b", {})

            files = list((tmp_path / "buffer").glob("events_testhost_*.jsonl"))
            assert len(files) == 2
            assert [e.sequence for e in b.get_pending_events()] == [1, 2]
        finally:
            b.close()

    def test_total_size_limit_drops_oldest_and_keeps_one(self, tmp_path):
        b = LocalEventBuffer(
            host_id="testhost",
            buffer_dir=str(tmp_path / "buffer"),
            max_file_size_mb=0,
            max_total_size_mb=0,
        )
        try:
            for i in range(4):
                b.buffer_event("e", {"i": i})

            # Retention never empties the buffer completely: one file always survives.
            files = list((tmp_path / "buffer").glob("events_testhost_*.jsonl"))
            assert len(files) == 1
            assert len(b.get_pending_events()) == 1
        finally:
            b.close()


class TestClose:
    def test_close_is_idempotent_and_reopens_on_next_write(self, buf):
        buf.buffer_event("a", {})
        buf.close()
        buf.close()

        assert buf.buffer_event("b", {}) == 2
        assert [e.event_type for e in buf.get_pending_events()] == ["a", "b"]
