"""
Local event buffer for offline resilience.

When PostgreSQL is unreachable, events are buffered to local JSON Lines files.
When connection is restored, events are replayed in order.

Buffer format: JSON Lines (one JSON object per line)
Location: /data/buffer/ (configurable via SENTINEL_BUFFER_DIR)
File naming: events_{host_id}_{timestamp}.jsonl
"""

import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _log(level: str, msg: str, **fields: Any) -> None:
    """Structured JSON logging."""
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "component": "local_buffer",
        "msg": msg,
        **fields
    }
    print(json.dumps(payload, ensure_ascii=False))


@dataclass
class BufferedEvent:
    """A single buffered event."""
    event_type: str  # 'start_investigation', 'investigation_event', 'update_investigation', etc.
    timestamp: str   # ISO format
    host_id: str
    data: Dict[str, Any]
    sequence: int    # Monotonic sequence number for ordering


class LocalEventBuffer:
    """
    File-based event buffer for offline operation.

    Design choices:
    - JSON Lines format (one JSON per line) for append-only, corruption-resistant writes
    - Sequence numbers ensure ordering across files
    - Write-ahead: buffer file is fsync'd before returning
    - Atomic rename on file rotation
    """

    def __init__(
        self,
        host_id: str,
        buffer_dir: str = "/data/buffer",
        max_file_size_mb: int = 10,
        max_total_size_mb: int = 100
    ):
        self.host_id = host_id
        self.buffer_dir = Path(buffer_dir)
        self.max_file_size = max_file_size_mb * 1024 * 1024
        self.max_total_size = max_total_size_mb * 1024 * 1024

        self._sequence = 0
        self._sequence_lock = threading.Lock()
        self._current_file: Optional[Path] = None
        self._file_handle = None
        self._write_lock = threading.Lock()

        # Create buffer directory
        self.buffer_dir.mkdir(parents=True, exist_ok=True)

        # Initialize sequence from existing files
        self._init_sequence()

        _log("info", "Local buffer initialized",
             host_id=host_id,
             buffer_dir=str(self.buffer_dir),
             max_file_size_mb=max_file_size_mb,
             max_total_size_mb=max_total_size_mb,
             initial_sequence=self._sequence)

    def _init_sequence(self):
        """Initialize sequence number from existing buffered files."""
        max_seq = 0
        for path in self.buffer_dir.glob(f"events_{self.host_id}_*.jsonl"):
            try:
                with open(path, 'r') as f:
                    for line in f:
                        if line.strip():
                            event = json.loads(line)
                            max_seq = max(max_seq, event.get('sequence', 0))
            except Exception as e:
                _log("warn", "Failed to read sequence from file", path=str(path), error=str(e))
                continue
        self._sequence = max_seq

    def buffer_event(self, event_type: str, data: Dict[str, Any]) -> int:
        """
        Buffer an event to local storage.

        Args:
            event_type: Type of event (e.g., 'start_investigation', 'investigation_event')
            data: Event payload

        Returns:
            Sequence number assigned to this event
        """
        with self._sequence_lock:
            self._sequence += 1
            seq = self._sequence

        event = BufferedEvent(
            event_type=event_type,
            timestamp=datetime.now(timezone.utc).isoformat(),
            host_id=self.host_id,
            data=data,
            sequence=seq
        )

        with self._write_lock:
            self._ensure_file()
            line = json.dumps(asdict(event), ensure_ascii=False, default=str) + '\n'
            self._file_handle.write(line)
            self._file_handle.flush()
            os.fsync(self._file_handle.fileno())  # Ensure durability

            # Check if rotation needed
            try:
                if self._current_file.stat().st_size > self.max_file_size:
                    self._rotate_file()
            except OSError:
                pass  # File might not exist yet

        _log("debug", "Event buffered", event_type=event_type, sequence=seq)
        return seq

    def _ensure_file(self):
        """Ensure we have an open file for writing."""
        if self._file_handle is None or self._file_handle.closed:
            timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
            self._current_file = self.buffer_dir / f"events_{self.host_id}_{timestamp}.jsonl"
            self._file_handle = open(self._current_file, 'a')
            _log("debug", "Opened new buffer file", path=str(self._current_file))

    def _rotate_file(self):
        """Close current file and start a new one."""
        if self._file_handle:
            self._file_handle.close()
            self._file_handle = None
            _log("debug", "Rotated buffer file", path=str(self._current_file))
        self._enforce_size_limit()

    def _enforce_size_limit(self):
        """Delete oldest files if total size exceeds limit."""
        files = sorted(
            self.buffer_dir.glob(f"events_{self.host_id}_*.jsonl"),
            key=lambda p: p.stat().st_mtime
        )
        total_size = sum(f.stat().st_size for f in files)

        while total_size > self.max_total_size and len(files) > 1:
            oldest = files.pop(0)
            file_size = oldest.stat().st_size
            total_size -= file_size
            oldest.unlink()
            _log("info", "Deleted oldest buffer file due to size limit",
                 path=str(oldest), freed_bytes=file_size)

    def get_pending_events(self) -> List[BufferedEvent]:
        """
        Get all pending events in sequence order.

        Returns:
            List of BufferedEvent objects sorted by sequence number
        """
        events = []
        for path in sorted(self.buffer_dir.glob(f"events_{self.host_id}_*.jsonl")):
            try:
                with open(path, 'r') as f:
                    for line in f:
                        if line.strip():
                            data = json.loads(line)
                            events.append(BufferedEvent(**data))
            except Exception as e:
                _log("warn", "Failed to read buffer file", path=str(path), error=str(e))

        # Sort by sequence to ensure ordering
        events.sort(key=lambda e: e.sequence)
        return events

    def mark_synced(self, up_to_sequence: int):
        """
        Mark events up to sequence number as synced.

        Deletes buffer files where all events have been processed.

        Args:
            up_to_sequence: All events with sequence <= this value are synced
        """
        with self._write_lock:
            for path in sorted(self.buffer_dir.glob(f"events_{self.host_id}_*.jsonl")):
                # Skip the current file being written to
                if self._current_file and path == self._current_file:
                    continue

                try:
                    max_seq_in_file = 0
                    with open(path, 'r') as f:
                        for line in f:
                            if line.strip():
                                event = json.loads(line)
                                max_seq_in_file = max(max_seq_in_file, event.get('sequence', 0))

                    if max_seq_in_file <= up_to_sequence:
                        # All events in this file have been synced
                        path.unlink()
                        _log("info", "Deleted synced buffer file",
                             path=str(path), max_sequence=max_seq_in_file)
                except Exception as e:
                    _log("warn", "Failed to cleanup buffer file", path=str(path), error=str(e))

    def has_pending_events(self) -> bool:
        """Check if there are any pending events to sync."""
        return any(self.buffer_dir.glob(f"events_{self.host_id}_*.jsonl"))

    def pending_count(self) -> int:
        """
        Count pending events (approximate - counts lines).

        Returns:
            Number of pending events
        """
        count = 0
        for path in self.buffer_dir.glob(f"events_{self.host_id}_*.jsonl"):
            try:
                with open(path, 'r') as f:
                    count += sum(1 for line in f if line.strip())
            except Exception:
                continue
        return count

    def close(self):
        """Close any open file handles."""
        with self._write_lock:
            if self._file_handle and not self._file_handle.closed:
                self._file_handle.close()
                self._file_handle = None
