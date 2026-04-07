"""Optional FastAPI adapter for the event runtime.

This module keeps FastAPI imports inside factory functions so the portable
stdlib runtime remains importable on systems where FastAPI is not installed.
"""

from __future__ import annotations

from typing import Any, Dict

from .bootstrap import build_portable_runtime
from .models import Alert, AlertSeverity


def _build_alert(payload: Dict[str, Any]) -> Alert:
    severity_value = str(payload.get("severity") or "info").lower()
    try:
        severity = AlertSeverity(severity_value)
    except ValueError as exc:
        raise ValueError(f"Invalid severity: {severity_value}") from exc

    summary = payload.get("summary")
    if not summary:
        raise ValueError("Missing required field: summary")

    return Alert(
        source=str(payload.get("source") or "manual"),
        severity=severity,
        summary=str(summary),
        details=dict(payload.get("details") or {}),
        namespace=payload.get("namespace"),
        resource_type=payload.get("resource_type"),
        resource_name=payload.get("resource_name"),
        fingerprint=payload.get("fingerprint"),
    )


def create_app(runtime=None):
    """Create a FastAPI app bound to the provided runtime.

    FastAPI is optional. Install it only when you want ASGI deployment:

        pip install fastapi uvicorn
    """

    try:
        from fastapi import FastAPI, HTTPException, Query
    except ImportError as exc:
        raise RuntimeError(
            "FastAPI is not installed. Install 'fastapi' and 'uvicorn' to use the ASGI adapter."
        ) from exc

    if runtime is None:
        runtime = build_portable_runtime()

    app = FastAPI(title="CFOperator Event Runtime", version="0.1.0")

    @app.get("/health")
    def health() -> dict:
        return runtime.health()

    @app.get("/history")
    def history(limit: int = Query(default=50, ge=1, le=500)) -> dict:
        return {"events": runtime.recent_events(limit=limit)}

    @app.post("/alert")
    def alert(payload: Dict[str, Any]) -> dict:
        try:
            normalized = _build_alert(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return runtime.handle_alert(normalized)

    return app


def build_app():
    """Factory entrypoint for uvicorn/gunicorn."""

    return create_app()