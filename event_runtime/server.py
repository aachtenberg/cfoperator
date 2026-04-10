"""Portable stdlib HTTP server for the event runtime."""

from __future__ import annotations

import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Tuple
from urllib.parse import parse_qs, urlparse

from .activity import render_activity_html
from .engine import EventRuntime
from .models import Alert
from .telemetry import render_metrics
from .worker import BackgroundAlertWorker, QueueFullError


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=True, default=str).encode("utf-8")
    _bytes_response(handler, status, data, "application/json")


def _bytes_response(handler: BaseHTTPRequestHandler, status: int, payload: bytes, content_type: str) -> None:
    data = payload
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def make_handler(runtime: EventRuntime, worker: BackgroundAlertWorker | None = None):
    """Create a request handler bound to the provided runtime."""

    class EventRuntimeHandler(BaseHTTPRequestHandler):
        server_version = "CFOperatorEventRuntime/0.1"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                payload = runtime.health()
                if worker is not None:
                    payload["worker"] = worker.health()
                _json_response(self, HTTPStatus.OK, payload)
                return
            if parsed.path == "/history":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["50"])[0])
                event_type = query.get("event_type", [""])[0] or None
                alert_id = query.get("alert_id", [""])[0] or None
                job_id = query.get("job_id", [""])[0] or None
                _json_response(
                    self,
                    HTTPStatus.OK,
                    {
                        "events": runtime.recent_events(
                            limit=limit,
                            event_type=event_type,
                            alert_id=alert_id,
                            job_id=job_id,
                        )
                    },
                )
                return
            if parsed.path == "/activity":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["25"])[0])
                status = query.get("status", [""])[0] or None
                action = query.get("action", [""])[0] or None
                event_limit = int(query.get("event_limit", [str(max(limit * 12, 100))])[0])
                _json_response(
                    self,
                    HTTPStatus.OK,
                    {
                        "activities": runtime.recent_activity(
                            limit=limit,
                            status=status,
                            action=action,
                            event_limit=event_limit,
                        )
                    },
                )
                return
            if parsed.path == "/activity.html":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["25"])[0])
                payload = render_activity_html(runtime.recent_activity(limit=limit))
                _bytes_response(self, HTTPStatus.OK, payload, "text/html; charset=utf-8")
                return
            if parsed.path == "/metrics":
                if worker is not None:
                    worker.refresh_metrics()
                payload, content_type = render_metrics()
                _bytes_response(self, HTTPStatus.OK, payload, content_type)
                return
            if parsed.path.startswith("/jobs/"):
                if worker is None:
                    _json_response(self, HTTPStatus.NOT_FOUND, {"error": "Worker not enabled"})
                    return
                job_id = parsed.path.rsplit("/", 1)[-1]
                job = worker.get_job(job_id)
                if job is None:
                    _json_response(self, HTTPStatus.NOT_FOUND, {"error": "Job not found"})
                    return
                _json_response(self, HTTPStatus.OK, job)
                return
            _json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/alert":
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length > 0 else b"{}"
                payload = json.loads(body.decode("utf-8"))
                alert = Alert.from_dict(payload)
                query = parse_qs(parsed.query)
                mode = query.get("mode", ["async" if worker else "sync"])[0]
                if worker is not None and mode != "sync":
                    job = worker.enqueue(alert)
                    _json_response(self, HTTPStatus.ACCEPTED, {"status": "queued", "job": job})
                else:
                    result = runtime.handle_alert(alert)
                    _json_response(self, HTTPStatus.OK, result)
            except json.JSONDecodeError:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON"})
            except QueueFullError as exc:
                _json_response(self, HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            except ValueError as exc:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def log_message(self, format: str, *args: Tuple[Any, ...]) -> None:
            return None

    return EventRuntimeHandler


logger = logging.getLogger(__name__)


def _start_poll_loop(
    runtime: EventRuntime,
    worker: BackgroundAlertWorker | None,
    interval_seconds: int,
    stop_event: threading.Event,
) -> threading.Thread | None:
    """Start a background thread that polls alert sources on an interval."""
    if not runtime.plugins.alert_sources:
        return None

    def _loop() -> None:
        while not stop_event.wait(interval_seconds):
            try:
                for source in runtime.plugins.alert_sources:
                    for alert in source.poll():
                        if worker is not None:
                            worker.enqueue(alert)
                        else:
                            runtime.handle_alert(alert)
            except Exception:
                logger.exception("Error during alert source poll cycle")

    thread = threading.Thread(target=_loop, daemon=True, name="event-runtime-poll")
    thread.start()
    return thread


def serve(
    runtime: EventRuntime,
    host: str = "0.0.0.0",
    port: int = 8080,
    worker: BackgroundAlertWorker | None = None,
    poll_interval_seconds: int = 30,
) -> None:
    """Start the portable threaded HTTP server."""
    runtime.start()
    if worker is not None:
        worker.start()
    stop_event = threading.Event()
    poll_thread = _start_poll_loop(runtime, worker, poll_interval_seconds, stop_event)
    handler = make_handler(runtime, worker=worker)
    server = ThreadingHTTPServer((host, port), handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()
        if poll_thread is not None:
            poll_thread.join(timeout=2)
        if worker is not None:
            worker.stop()
        runtime.stop()