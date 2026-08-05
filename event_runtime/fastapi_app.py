"""Optional FastAPI adapter for the event runtime.

This module keeps FastAPI imports inside factory functions so the portable
stdlib runtime remains importable on systems where FastAPI is not installed.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Dict

from .activity import render_activity_html
from .bootstrap import build_portable_runtime, build_portable_worker
from .http_actions import (
    COMPLETION_AUTH_HEADER,
    parse_completion_payload,
    verify_completion_auth,
    verify_runtime_auth,
)
from .models import Alert
from .telemetry import observe_completion_request
from .worker import QueueFullError


def create_app(runtime=None, worker=None):
    """Create a FastAPI app bound to the provided runtime.

    FastAPI is optional. Install it only when you want ASGI deployment:

        pip install fastapi uvicorn
    """

    try:
        from fastapi import FastAPI, HTTPException, Query, Request, Response
        from fastapi.responses import JSONResponse
    except ImportError as exc:
        raise RuntimeError(
            "FastAPI is not installed. Install 'fastapi' and 'uvicorn' to use the ASGI adapter."
        ) from exc

    if runtime is None:
        runtime = build_portable_runtime()
    if worker is None:
        worker = build_portable_worker(runtime)

    @asynccontextmanager
    async def lifespan(app):
        runtime.start()
        if worker is not None:
            worker.start()
        try:
            yield
        finally:
            if worker is not None:
                worker.stop()
            runtime.stop()

    app = FastAPI(title="CFOperator Event Runtime", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def runtime_auth(request: Request, call_next):
        """Bearer gate over the whole surface; a no-op unless CFOP_RUNTIME_TOKEN
        is set. Kept as middleware so a route added later is covered by
        default rather than opted in."""
        error = verify_runtime_auth(request.url.path, request.headers.get("Authorization"))
        if error is not None:
            return JSONResponse({"detail": error}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        return await call_next(request)

    @app.get("/livez")
    def livez() -> dict:
        # Liveness only: must never touch the runtime (health() does a
        # live jobstore query, so a slow DB would get the pod killed).
        return {"status": "alive"}

    @app.get("/health")
    def health() -> dict:
        payload = runtime.health()
        if worker is not None:
            payload["worker"] = worker.health()
        return payload

    @app.get("/history")
    def history(
        limit: int = Query(default=50, ge=1, le=500),
        event_type: str | None = Query(default=None),
        alert_id: str | None = Query(default=None),
        job_id: str | None = Query(default=None),
    ) -> dict:
        return {
            "events": runtime.recent_events(
                limit=limit,
                event_type=event_type,
                alert_id=alert_id,
                job_id=job_id,
            )
        }

    @app.get("/activity")
    def activity(
        limit: int = Query(default=25, ge=1, le=250),
        status: str | None = Query(default=None),
        action: str | None = Query(default=None),
        event_limit: int | None = Query(default=None, ge=1, le=5000),
    ) -> dict:
        return {
            "activities": runtime.recent_activity(
                limit=limit,
                status=status,
                action=action,
                event_limit=event_limit,
            )
        }

    @app.get("/scheduled")
    def scheduled(
        limit: int = Query(default=100, ge=1, le=1000),
        scheduler: str | None = Query(default=None),
    ) -> dict:
        return {"scheduled_tasks": runtime.scheduled_tasks(limit=limit, scheduler_name=scheduler)}

    @app.get("/activity.html")
    def activity_html(limit: int = Query(default=25, ge=1, le=250)) -> Response:
        payload = render_activity_html(runtime.recent_activity(limit=limit))
        return Response(content=payload, media_type="text/html")

    @app.get("/metrics")
    def metrics() -> Response:
        from .telemetry import render_metrics

        if worker is not None:
            worker.refresh_metrics()
        payload, content_type = render_metrics()
        return Response(content=payload, media_type=content_type)

    @app.get("/jobs/{job_id}")
    def job(job_id: str) -> dict:
        if worker is None:
            raise HTTPException(status_code=404, detail="Worker not enabled")
        payload = worker.get_job(job_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return payload

    @app.post("/alert")
    def alert(payload: Dict[str, Any], mode: str = Query(default="async")) -> dict:
        try:
            normalized = Alert.from_dict(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if worker is not None and mode != "sync":
            try:
                return {"status": "queued", "job": worker.enqueue(normalized)}
            except QueueFullError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        return runtime.handle_alert(normalized)

    @app.post("/v1/investigations/{alert_id}/complete")
    def investigation_complete(alert_id: str, payload: Dict[str, Any], request: Request) -> dict:
        """Receive a completed ActionResult from an external executor (the agent)."""
        auth_error = verify_completion_auth(request.headers.get(COMPLETION_AUTH_HEADER))
        if auth_error is not None:
            outcome = "auth_missing" if "Missing" in auth_error else "auth_invalid"
            observe_completion_request(outcome)
            raise HTTPException(status_code=401, detail=auth_error)
        try:
            alert, action_result = parse_completion_payload(payload, alert_id)
        except ValueError as exc:
            observe_completion_request("bad_request")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        runtime.record_external_action_completion(alert, action_result)
        observe_completion_request("recorded")
        return {"status": "recorded", "alert_id": alert_id}

    return app


def build_app():
    """Factory entrypoint for uvicorn/gunicorn."""

    return create_app()