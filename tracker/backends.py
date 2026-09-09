"""Backend registry for the tracker service.

One image, the backend chosen by ``CFOP_TRACKER_BACKEND``. This is a deliberate
deviation from changerecord's image-per-backend model: three stdlib REST
adapters of ~100 lines each do not earn three Dockerfiles and three CI jobs,
and compose/Helm can then pick a backend by value. The HTTP contract in
``entrypoint.py`` stays backend-free, so an image swap remains possible later.

A backend is anything with ``name`` and the four methods below. ``meta`` is the
decoded ref token — ``{"backend": <name>, ...id fields}`` — already checked by
the router to belong to the running backend.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Protocol

from shapes import Item, ItemRef, ItemState


class TrackerError(RuntimeError):
    """The backend refused or could not proceed (rendered as 400)."""


class TrackerNotFound(TrackerError):
    """The item behind a ref no longer exists on the backend (rendered as 404)."""


class Backend(Protocol):
    name: str

    def create(self, item: Item) -> ItemRef: ...

    def comment(self, meta: Dict[str, Any], body_markdown: str) -> None: ...

    def transition(self, meta: Dict[str, Any], state: str, note: str) -> None: ...

    def get(self, meta: Dict[str, Any]) -> ItemState: ...


def require_env(env: Dict[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise TrackerError(f"{name} required")
    return value


def _make_plane(env: Dict[str, str]) -> Backend:
    from backend_plane import make_plane
    return make_plane(env)


def _make_github(env: Dict[str, str]) -> Backend:
    from backend_github import make_github
    return make_github(env)


def _make_jira(env: Dict[str, str]) -> Backend:
    from backend_jira import make_jira
    return make_jira(env)


BACKENDS: Dict[str, Callable[[Dict[str, str]], Backend]] = {
    "plane": _make_plane,
    "github": _make_github,
    "jira": _make_jira,
}

BACKEND_ENV = "CFOP_TRACKER_BACKEND"


def make_backend(env: Dict[str, str]) -> Backend:
    """Build the configured backend, or raise ``TrackerError`` naming what is missing."""
    name = (env.get(BACKEND_ENV) or "").strip().lower()
    if not name:
        raise TrackerError(f"{BACKEND_ENV} required (one of {', '.join(sorted(BACKENDS))})")
    maker = BACKENDS.get(name)
    if maker is None:
        raise TrackerError(f"unknown {BACKEND_ENV} {name!r} (one of {', '.join(sorted(BACKENDS))})")
    return maker(env)
