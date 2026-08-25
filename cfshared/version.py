"""The one place the running build says what it is (CFOP-92).

The version is baked into the image at build time: the release workflow
passes the image tag as the ``CFOP_VERSION`` build-arg and the Dockerfile
promotes it to an ENV, so ``/api/health``, the ``cfoperator_agent_info``
metric and the startup log all report the tag that was actually pulled —
``1.1.0`` for a release, ``main-1a551b7`` for a main build. Nothing here is
hand-edited on release, which is the whole point: a literal in the code is
exactly what went stale four times over before this existed.

Unset means the code is running from source, or from an image built without
the arg (``docker compose build`` for the trial stack and the demo). That is
reported as ``dev`` — honest, and impossible to mistake for a release.
"""

import os

VERSION_ENV = "CFOP_VERSION"
DEV_VERSION = "dev"


def build_version() -> str:
    """The baked build version, or ``dev`` when nothing baked one."""
    return os.environ.get(VERSION_ENV, "").strip() or DEV_VERSION
