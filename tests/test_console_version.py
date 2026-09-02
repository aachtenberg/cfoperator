"""One build-time source for the console version (CFOP-92).

``v1.0.8`` used to be a string literal in four places — ``/api/health`` and
three spots in ``index.html`` — none derived from a build, so a release could
ship showing the previous number and nothing would notice. Now the version is
baked into the image: the build workflow passes the image tag as the
``CFOP_VERSION`` build-arg, the Dockerfile promotes it to an ENV,
``cfshared.version.build_version()`` reads it, and the console renders
whatever ``/api/health`` says.

These guard the class of regression, not today's string: a literal creeping
back into the UI or the health handler, the handler no longer reading the
baked source, or the plumbing that bakes it (build-arg, ARG, ENV) being
dropped — which is the failure that stays silent longest, because the code
still runs and just says ``dev`` forever.
"""

from repo_paths import REPO_ROOT
import os
import re
import threading
import time
from pathlib import Path

import pytest
import yaml

from cfshared.version import DEV_VERSION, VERSION_ENV, build_version

ROOT = REPO_ROOT
UI = ROOT / "ui"
WEB_SERVER = ROOT / "web_server.py"
DOCKERFILE = ROOT / "Dockerfile"
BUILD_WORKFLOW = ROOT / ".github" / "workflows" / "build-cfoperator-main.yml"


# --------------------------------------------------------------------------
# the resolver
# --------------------------------------------------------------------------

def test_build_version_reads_the_baked_env(monkeypatch):
    monkeypatch.setenv(VERSION_ENV, "main-1a551b7")
    assert build_version() == "main-1a551b7"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_build_version_falls_back_to_dev_when_nothing_baked(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(VERSION_ENV, raising=False)
    else:
        monkeypatch.setenv(VERSION_ENV, value)
    assert build_version() == DEV_VERSION


# --------------------------------------------------------------------------
# /api/health on the real routes
# --------------------------------------------------------------------------

def _client():
    """The real WebServer routes over a stub operator, auth disabled — the
    same shape test_cockpit_open.py uses, trimmed to what /api/health needs."""
    from unittest.mock import MagicMock

    from flask import Flask

    from web_auth import install_auth
    from web_server import WebServer

    operator = MagicMock()
    operator.current_investigation = None
    operator.start_time = time.time()
    operator.config = {}

    server = WebServer.__new__(WebServer)
    server.operator = operator
    server.host, server.port = "localhost", 0
    server.app = Flask(__name__)
    server._chat_sessions = {}
    server._sessions_lock = threading.Lock()
    server.auth_store = None
    server._cockpit = None
    server._ladder = None
    server._setup_routes()

    prior = {k: os.environ.get(k) for k in
             ("CFOP_AUTH_DISABLED", "CFOP_SESSION_SECRET", "CFOP_UI_USERNAME",
              "CFOP_UI_PASSWORD_HASH", "CFOP_API_TOKEN")}
    os.environ["CFOP_AUTH_DISABLED"] = "1"
    os.environ["CFOP_SESSION_SECRET"] = "test-session-secret"
    for name in ("CFOP_UI_USERNAME", "CFOP_UI_PASSWORD_HASH", "CFOP_API_TOKEN"):
        os.environ[name] = ""
    try:
        install_auth(server.app, ui_dir="ui", store=None)
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return server.app.test_client()


def test_health_reports_the_baked_version(monkeypatch):
    """The wire value is the baked source, read at request time — not a
    module constant captured at import, so a test can vary it and so can a
    deploy that overrides the env."""
    monkeypatch.setenv(VERSION_ENV, "9.9.9")
    body = _client().get("/api/health").get_json()
    assert body["status"] == "ok"
    assert body["version"] == "9.9.9"
    assert "uptime_seconds" in body  # the field the status bar already used


def test_health_says_dev_from_source(monkeypatch):
    monkeypatch.delenv(VERSION_ENV, raising=False)
    assert _client().get("/api/health").get_json()["version"] == DEV_VERSION


# --------------------------------------------------------------------------
# no literal anywhere it used to live
# --------------------------------------------------------------------------

PRODUCT_VERSION_LITERAL = re.compile(r"CFOperator\s+v\d", re.IGNORECASE)
STATUS_VERSION_PREFILLED = re.compile(r'id="status-version"[^>]*>\s*[^<\s]')
HEALTH_VERSION_LITERAL = re.compile(r"""['"]version['"]\s*:\s*['"]\d""")


def test_no_version_literal_in_ui():
    offenders = []
    for path in sorted(UI.iterdir()):
        if path.suffix not in (".html", ".js"):
            continue
        text = path.read_text(encoding="utf-8")
        if PRODUCT_VERSION_LITERAL.search(text):
            offenders.append(f"{path.name}: greeting carries a version literal")
        if STATUS_VERSION_PREFILLED.search(text):
            offenders.append(f"{path.name}: #status-version is pre-filled in markup")
    assert not offenders, "\n".join(offenders)


def test_no_version_literal_in_web_server():
    src = WEB_SERVER.read_text(encoding="utf-8")
    assert not HEALTH_VERSION_LITERAL.search(src), (
        "web_server.py answers /api/health with a literal version; "
        "it must come from cfshared.version.build_version()")
    assert "build_version()" in src


def test_console_renders_status_version_from_health():
    """updateStatus() is the one fetch of /api/health the page makes; the
    status bar must take its version from that response."""
    html = (UI / "index.html").read_text(encoding="utf-8")
    start = html.index("function updateStatus()")
    end = html.index("setInterval(updateStatus", start)
    body = html[start:end]
    assert "/api/health" in body
    assert "data.version" in body
    assert "status-version" in body


# --------------------------------------------------------------------------
# the plumbing that bakes it
# --------------------------------------------------------------------------

def test_dockerfile_bakes_the_version():
    """ARG before ENV, and the ENV named for the resolver's env var. A default
    of "dev" so a build without the arg is honest rather than empty."""
    lines = DOCKERFILE.read_text(encoding="utf-8").splitlines()
    arg = next((i for i, l in enumerate(lines)
                if re.match(rf"^ARG {VERSION_ENV}={DEV_VERSION}\s*$", l)), None)
    env = next((i for i, l in enumerate(lines)
                if re.match(rf"^ENV {VERSION_ENV}=\$\{{{VERSION_ENV}\}}\s*$", l)), None)
    assert arg is not None, f"Dockerfile must declare ARG {VERSION_ENV}={DEV_VERSION}"
    assert env is not None, f"Dockerfile must promote it: ENV {VERSION_ENV}=${{{VERSION_ENV}}}"
    assert arg < env, "ARG must precede the ENV that reads it"


def test_build_workflow_passes_the_image_tag_as_the_version():
    """The build-arg is derived from the same step output that names the
    image, so the version /api/health reports is the tag that was pulled."""
    wf = yaml.safe_load(BUILD_WORKFLOW.read_text(encoding="utf-8"))
    steps = wf["jobs"]["build-and-push"]["steps"]
    tag_step = next(s for s in steps if s.get("id") == "tag")
    build_step = next(s for s in steps if s.get("uses", "").startswith("docker/build-push-action"))

    # The version output is the tag with only its leading v removed.
    assert re.search(r'^\s*echo "version=\$\{tag#v\}" >> "\$GITHUB_OUTPUT"\s*$',
                     tag_step["run"], re.M), tag_step["run"]
    # ...and the tag that names the image is the same shell variable.
    assert re.search(r'^\s*echo "tag=\$tag" >> "\$GITHUB_OUTPUT"\s*$', tag_step["run"], re.M)

    build_args = build_step["with"].get("build-args", "")
    assert f"{VERSION_ENV}=${{{{ steps.tag.outputs.version }}}}" in build_args, (
        f"build-and-push must pass {VERSION_ENV} from steps.tag.outputs.version; got {build_args!r}")
