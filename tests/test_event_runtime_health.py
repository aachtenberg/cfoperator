"""/health answers on time, and a client that left is not a crash (CFOP-768).

What happened: the node running the runtime lost its network link for ten
minutes. The event-runtime's readiness probe is ``GET /health``, and health
asks the scheduler, whose jobstore query sat on a connection that would never
answer. Each probe timed out and the next one started another blocked thread.
When the link came back, sixty of them wrote their answers to sockets the
kubelet had closed long before, socketserver printed a traceback for each, and
the traceback-rate alert reported a crashing runtime.

Two separate defects, so two guards, plus the number that ties the runtime to
the chart:

* a health check that hangs must not make ``health()`` hang, and must not cost
  a thread per caller;
* a write to a client that has gone must not reach socketserver's
  ``handle_error``;
* the chart's readiness probe has to wait longer than the runtime does.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from http.server import ThreadingHTTPServer

import yaml

from event_runtime import engine as engine_module
from event_runtime.deadline import DeadlineExceeded, SingleFlight
from event_runtime.engine import EventRuntime
from event_runtime.models import Decision, ScheduledTask
from event_runtime.plugin_manager import PluginManager
from event_runtime.plugins import DecisionEngine, Scheduler
from event_runtime.server import make_handler
from event_runtime.state.composite import CompositeStateSink
from event_runtime.state.local_outbox import LocalOutboxStateSink
from repo_paths import REPO_ROOT


class _Decision(DecisionEngine):
    name = "decision"

    def decide(self, envelope):
        return Decision(action="log_only", confidence=1.0, reasoning="", params={}, requested_checks=[])


class _HangingScheduler(Scheduler):
    """A scheduler whose health check blocks until the test lets it go."""

    name = "hanging"

    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def schedule(self, task: ScheduledTask):
        return {}

    def health(self):
        with self._lock:
            self.calls += 1
        self.entered.set()
        self.release.wait(30)
        return {"name": self.name, "jobs_count": 7}


class _RaisingScheduler(Scheduler):
    name = "raising"

    def schedule(self, task: ScheduledTask):
        return {}

    def health(self):
        raise RuntimeError("jobstore refused the connection")


def _runtime(tmp_path, *schedulers, timeout=0.2):
    plugins = PluginManager()
    plugins.register_state_sink(CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "outbox"))]))
    plugins.register_decision_engine(_Decision())
    for scheduler in schedulers:
        plugins.register_scheduler(scheduler)
    runtime = EventRuntime(plugins)
    runtime.health_call_timeout_seconds = timeout
    return runtime


def test_a_hanging_scheduler_does_not_hang_health(tmp_path):
    scheduler = _HangingScheduler()
    runtime = _runtime(tmp_path, scheduler)
    try:
        started = time.monotonic()
        payload = runtime.health()
        elapsed = time.monotonic() - started

        assert scheduler.entered.is_set(), "the scheduler was never asked, so nothing was bounded"
        assert elapsed < 2.0, f"health() took {elapsed:.1f}s behind a 0.2s deadline"
        assert payload["scheduler"] == [{"name": "hanging", "error": "health check timed out"}]
        # The rest of the payload is still there: a stuck jobstore is one
        # entry, not the whole answer.
        assert payload["schedulers"] == ["hanging"]
        assert "sink" in payload
    finally:
        scheduler.release.set()


def test_probes_during_a_hang_share_one_call(tmp_path):
    """The pile-up itself: sixty probes must not be sixty parked threads."""
    scheduler = _HangingScheduler()
    # A name of its own, so the count below is of this test's threads only.
    # active_count() would also see another test's call still winding down.
    scheduler.name = "pile-up"
    runtime = _runtime(tmp_path, scheduler, timeout=0.05)
    try:
        for _ in range(10):
            runtime.health()
        assert scheduler.calls == 1, f"{scheduler.calls} health calls were started while the first was stuck"
        parked = [t for t in threading.enumerate() if t.name == "health-pile-up"]
        assert len(parked) == 1
    finally:
        scheduler.release.set()


def test_health_recovers_once_the_stuck_call_returns(tmp_path):
    scheduler = _HangingScheduler()
    runtime = _runtime(tmp_path, scheduler)
    assert "error" in runtime.health()["scheduler"][0]

    scheduler.release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = runtime.health()["scheduler"][0]
        if "error" not in state:
            break
        time.sleep(0.02)
    assert state == {"name": "hanging", "jobs_count": 7}
    # A finished call is not a cache: the next probe asks the scheduler again.
    asked = scheduler.calls
    assert runtime.health()["scheduler"][0] == {"name": "hanging", "jobs_count": 7}
    assert scheduler.calls == asked + 1


def test_two_slow_schedulers_share_the_deadline(tmp_path):
    first, second = _HangingScheduler(), _HangingScheduler()
    second.name = "hanging-too"
    runtime = _runtime(tmp_path, first, second, timeout=0.3)
    try:
        started = time.monotonic()
        payload = runtime.health()
        elapsed = time.monotonic() - started
        assert [entry["error"] for entry in payload["scheduler"]] == ["health check timed out"] * 2
        assert elapsed < 0.55, f"two schedulers took {elapsed:.2f}s behind one 0.3s deadline"
    finally:
        first.release.set()
        second.release.set()


def test_a_raising_scheduler_still_reads_as_failed(tmp_path):
    # The behaviour before this change, kept: quick failures were never the bug.
    runtime = _runtime(tmp_path, _RaisingScheduler())
    assert runtime.health()["scheduler"] == [{"name": "raising", "error": "health check failed"}]


def test_a_hang_is_logged_once_not_once_per_probe(tmp_path, caplog):
    scheduler = _HangingScheduler()
    runtime = _runtime(tmp_path, scheduler, timeout=0.05)
    try:
        with caplog.at_level(logging.WARNING, logger="event_runtime.engine"):
            for _ in range(5):
                runtime.health()
        hangs = [r for r in caplog.records if "did not answer its health check" in r.getMessage()]
        assert len(hangs) == 1
    finally:
        scheduler.release.set()


def test_single_flight_hands_the_error_to_every_waiter():
    gate = threading.Event()

    def fail():
        gate.wait(5)
        raise ValueError("no")

    flight = SingleFlight(fail)
    errors = []

    def wait():
        try:
            flight.call(5)
        except ValueError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=wait) for _ in range(3)]
    for thread in threads:
        thread.start()
    time.sleep(0.05)
    gate.set()
    for thread in threads:
        thread.join(5)
    assert len(errors) == 3
    assert len({id(error) for error in errors}) == 1, "each waiter started its own call"


def test_single_flight_deadline_leaves_the_call_running():
    gate = threading.Event()
    flight = SingleFlight(lambda: gate.wait(5) and "done")
    try:
        flight.call(0.05)
    except DeadlineExceeded:
        pass
    else:
        raise AssertionError("a call that had not returned was reported as answered")
    gate.set()
    assert flight.call(5) == "done"


# ---- a client that left ---------------------------------------------------


class _RecordingServer(ThreadingHTTPServer):
    """socketserver calls handle_error for any exception a handler lets out,
    and its default prints the traceback the alert counts."""

    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.errors = []
        self.handled = threading.Event()

    def handle_error(self, request, client_address):
        import sys
        self.errors.append(sys.exc_info()[1])

    def shutdown_request(self, request):
        super().shutdown_request(request)
        self.handled.set()


def _send(port: int, request_line: bytes) -> socket.socket:
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    client.sendall(request_line + b"\r\nHost: test\r\n\r\n")
    return client


def _hang_up(client: socket.socket) -> None:
    """Leave without reading, with a reset, not a FIN.

    SO_LINGER 0 makes close() send RST, so the server's first write fails.
    After a plain close the first write succeeds and only the second one
    learns the peer has gone; that also fails here today, because headers and
    body are two writes, but it would stop failing if they became one.
    """
    client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    client.close()


def test_a_probe_that_gave_up_is_one_log_line_not_a_traceback(tmp_path, caplog):
    scheduler = _HangingScheduler()
    # Long deadline: the handler must still be inside health() when the client
    # leaves, which is the order things happened in.
    runtime = _runtime(tmp_path, scheduler, timeout=10)
    server = _RecordingServer(("127.0.0.1", 0), make_handler(runtime))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with caplog.at_level(logging.INFO, logger="event_runtime.server"):
            client = _send(server.server_address[1], b"GET /health HTTP/1.1")
            assert scheduler.entered.wait(5), "the request never reached health()"
            _hang_up(client)
            time.sleep(0.1)  # let the reset arrive before the answer is written
            scheduler.release.set()
            assert server.handled.wait(5), "the handler never finished"

        assert server.errors == [], f"the handler let out {server.errors!r}"
        gone = [r for r in caplog.records if "client closed the connection" in r.getMessage()]
        assert len(gone) == 1, [r.getMessage() for r in caplog.records]
        assert gone[0].levelno == logging.INFO
        assert "/health" in gone[0].getMessage()
    finally:
        scheduler.release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_the_recording_server_does_see_a_handler_that_raises(tmp_path):
    """The test above asserts an empty list. This is what fills it."""
    runtime = _runtime(tmp_path)

    def boom():
        raise BrokenPipeError(32, "Broken pipe")

    runtime.health = boom
    server = _RecordingServer(("127.0.0.1", 0), make_handler(runtime))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = _send(server.server_address[1], b"GET /health HTTP/1.1")
        assert server.handled.wait(5)
        client.close()
        assert len(server.errors) == 1 and isinstance(server.errors[0], BrokenPipeError)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


# ---- the chart ------------------------------------------------------------


def _event_runtime_readiness_probe() -> dict:
    """The readinessProbe block from the chart template.

    The template is not YAML until helm renders it, so cut the block out by
    indentation; the probe itself carries no template syntax.
    """
    lines = (REPO_ROOT / "charts" / "cfoperator" / "templates" / "event-runtime.yaml").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "readinessProbe:")
    indent = len(lines[start]) - len(lines[start].lstrip())
    block = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        block.append(line)
    return yaml.safe_load("\n".join(block))["readinessProbe"]


def test_the_charts_probe_waits_longer_than_the_runtime_does():
    probe = _event_runtime_readiness_probe()
    assert probe["httpGet"]["path"] == "/health"
    # Kubernetes' default is 1s when the key is absent, which is the trap: the
    # chart shipped that way, under the runtime's own deadline.
    timeout = probe.get("timeoutSeconds", 1)
    assert timeout > engine_module.HEALTH_CALL_TIMEOUT_SECONDS, (
        f"the readiness probe waits {timeout}s but /health may take "
        f"{engine_module.HEALTH_CALL_TIMEOUT_SECONDS}s to report a stuck scheduler; "
        "the kubelet would hang up first"
    )
