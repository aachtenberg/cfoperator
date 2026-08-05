"""Authentication for the agent web console on :8083.

Why this exists
---------------
The agent runs hostNetwork on the k3s node, so :8083 is bound to the node's
LAN interface directly. It exposes 51 routes, 26 of them POST, including
``/api/remediations/<id>/approve`` which flips a row to ``queued`` and hands it
straight to the executor. None of it was authenticated. The only thing standing
in front was a host firewall (ansible/deploy-cfoperator-8083-guard.yml in
homelab-infra) allowlisting a handful of source IPs, which does not survive the
console being opened to the whole LAN.

Two callers, two mechanisms
---------------------------
* **Browsers** get a signed session cookie from a login form. The UI is static
  HTML that calls ``/api/*`` with same-origin fetches, so the cookie rides along
  automatically and no page needs changing.
* **Services** (event_runtime, cfoperator-mcp, bridge) send
  ``Authorization: Bearer $CFOP_API_TOKEN``. They are not interactive and must
  never be redirected to a login page.

Exempt paths
------------
``/api/health`` (kubelet probes) and ``/metrics`` (Prometheus scrape) stay open.
Both are read-only and non-sensitive, and gating them would break liveness
checks and monitoring for no benefit.

Failure mode is CLOSED
----------------------
If the console is configured for auth but the credentials are missing, every
non-exempt route returns 503 rather than falling open. The deploy order is
secrets first, then image, so this window should not occur — but "misconfigured"
must never silently mean "unprotected".

``CFOP_AUTH_DISABLED=true`` bypasses everything for local docker-compose work.
It logs a loud warning on every start so it cannot be set in a deployed
environment unnoticed.
"""

import functools
import logging
import os
import re
import secrets
import time

from flask import (
    jsonify,
    redirect,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

logger = logging.getLogger(__name__)

# Routes that must answer without credentials. Keep this list minimal and
# read-only — anything added here is reachable by every device on the LAN.
EXEMPT_PATHS = frozenset({"/api/health", "/metrics", "/login", "/logout"})

# The only routes that authenticate with the X-CFOP-Token completion secret —
# they call verify_completion_auth() themselves. The console gate honors that
# secret ONLY on these paths; on every other route it must be ignored, so the
# machine secret (injected into disposable executor Jobs and the deep-
# investigation worker) can never act as a full console credential — in
# particular it must not reach /api/remediations/<id>/approve. Keep in sync
# with the verify_completion_auth routes in web_server.py.
_COMPLETION_TOKEN_PATHS = re.compile(
    r"^/v1/(deep-investigations"
    r"|remediations/(\d+/complete|feed-sweeps|feed-summary))/?$"
)

# Brute force throttle. The login form is reachable from every LAN device once
# the firewall opens up, and a single shared password is a realistic target.
# Deliberately in-memory: a restart clearing the counters is acceptable, and it
# avoids a database round trip on a hot path.
_MAX_FAILURES = 8
_LOCKOUT_SECONDS = 300
_failures: dict[str, list] = {}


def _client_ip() -> str:
    """Best-effort source IP. No proxy sits in front of :8083 (hostNetwork,
    reached directly), so remote_addr is the real client and X-Forwarded-For
    would be attacker-controlled — do not trust it here."""
    return request.remote_addr or "unknown"


def _locked_out(ip: str) -> bool:
    entry = _failures.get(ip)
    if not entry:
        return False
    count, first_seen = entry
    if time.time() - first_seen > _LOCKOUT_SECONDS:
        _failures.pop(ip, None)
        return False
    return count >= _MAX_FAILURES


def _record_failure(ip: str) -> None:
    now = time.time()
    entry = _failures.get(ip)
    if not entry or now - entry[1] > _LOCKOUT_SECONDS:
        _failures[ip] = [1, now]
    else:
        entry[0] += 1


def _wants_html() -> bool:
    """A browser navigating gets a redirect; an API client gets 401 JSON.

    Checked via the Accept header rather than the path, because the UI's own
    fetch() calls hit the same /api/* routes a script would."""
    accept = request.headers.get("Accept", "")
    return "text/html" in accept and "application/json" not in accept


class ConsoleAuth:
    """Holds the configured credentials and installs the Flask hooks."""

    def __init__(self, app, ui_dir: str = "ui"):
        self.app = app
        self.ui_dir = ui_dir
        self.disabled = os.getenv("CFOP_AUTH_DISABLED", "").lower() in ("1", "true", "yes")
        self.username = os.getenv("CFOP_UI_USERNAME", "")
        self.password_hash = os.getenv("CFOP_UI_PASSWORD_HASH", "")
        self.api_token = os.getenv("CFOP_API_TOKEN", "")

        session_secret = os.getenv("CFOP_SESSION_SECRET", "")
        if session_secret:
            app.secret_key = session_secret
        elif not self.disabled:
            # Without a stable secret, cookies would be invalidated on every
            # restart AND every replica would sign differently.
            logger.error("CFOP_SESSION_SECRET is not set — sessions cannot be signed")

        app.config.update(
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SAMESITE="Lax",
            # NOT Secure: :8083 is plain HTTP on the LAN with no TLS in front.
            # Setting this would make the cookie undeliverable and lock the UI
            # out entirely.
            SESSION_COOKIE_SECURE=False,
        )

        if self.disabled:
            logger.warning(
                "CONSOLE AUTH DISABLED (CFOP_AUTH_DISABLED) — :8083 is fully open. "
                "This must only ever be set for local development."
            )
        elif not self.configured:
            logger.error(
                "Console auth is enabled but unconfigured (need CFOP_UI_USERNAME, "
                "CFOP_UI_PASSWORD_HASH, CFOP_API_TOKEN) — all non-exempt routes "
                "will return 503 until these are set."
            )
        else:
            logger.info("Console auth active (user=%s, service token configured)", self.username)

    @property
    def configured(self) -> bool:
        return bool(self.username and self.password_hash and self.api_token)

    # ---- request gate -------------------------------------------------

    def _service_token_ok(self) -> bool:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        presented = header[7:].strip()
        if not presented or not self.api_token:
            return False
        # Timing-safe: a naive == leaks the token prefix byte by byte.
        return secrets.compare_digest(presented, self.api_token)

    def _completion_token_ok(self) -> bool:
        """Machine callbacks (executor Jobs, deep-investigation workers) present
        the X-CFOP-Token shared secret, not the console bearer token. Honor it
        here so this gate doesn't 401 them before their route-level
        verify_completion_auth() runs.

        Scoped to the completion callback paths only: this secret is broadly
        distributed (every executor Job pod, the deep-investigation worker), so
        accepting it on any other route would turn the lowest-privilege
        credential in the system into a full console credential. And only when
        the secret is configured — an unset secret must not become a bypass."""
        if not _COMPLETION_TOKEN_PATHS.match(request.path):
            return False

        from event_runtime.http_actions import COMPLETION_AUTH_HEADER, COMPLETION_SECRET_ENV

        expected = os.getenv(COMPLETION_SECRET_ENV, "").strip()
        presented = request.headers.get(COMPLETION_AUTH_HEADER, "")
        if not expected or not presented:
            return False
        return secrets.compare_digest(presented, expected)

    def check_request(self):
        """before_request hook. Returning None lets the request proceed."""
        if self.disabled:
            return None

        path = request.path
        if path in EXEMPT_PATHS:
            return None

        if not self.configured:
            return jsonify({
                "error": "console auth is not configured",
                "detail": "CFOP_UI_USERNAME / CFOP_UI_PASSWORD_HASH / CFOP_API_TOKEN are unset",
            }), 503

        if self._service_token_ok():
            return None

        if self._completion_token_ok():
            return None

        if session.get("cfop_user"):
            return None

        if _wants_html():
            return redirect(url_for("login", next=request.full_path))
        return jsonify({"error": "authentication required"}), 401

    # ---- routes -------------------------------------------------------

    def register(self):
        app = self.app
        app.before_request(self.check_request)

        @app.route("/login", methods=["GET"])
        def login():
            if self.disabled or session.get("cfop_user"):
                return redirect("/")
            return send_from_directory(self.ui_dir, "login.html")

        @app.route("/login", methods=["POST"])
        def login_post():
            ip = _client_ip()
            if _locked_out(ip):
                logger.warning("login locked out for %s", ip)
                return jsonify({"error": "too many attempts, try again later"}), 429

            if not self.configured:
                return jsonify({"error": "console auth is not configured"}), 503

            data = request.get_json(silent=True) or request.form
            user = (data.get("username") or "").strip()
            password = data.get("password") or ""

            # Compare BOTH factors before answering, and give one generic error,
            # so a wrong username is indistinguishable from a wrong password.
            user_ok = secrets.compare_digest(user, self.username)
            pass_ok = check_password_hash(self.password_hash, password)

            if not (user_ok and pass_ok):
                _record_failure(ip)
                logger.warning("failed console login from %s (user=%r)", ip, user)
                return jsonify({"error": "invalid credentials"}), 401

            _failures.pop(ip, None)
            session.clear()
            session["cfop_user"] = self.username
            session.permanent = True
            logger.info("console login from %s", ip)
            return jsonify({"ok": True, "next": data.get("next") or "/"})

        @app.route("/logout", methods=["GET", "POST"])
        def logout():
            session.clear()
            if _wants_html():
                return redirect(url_for("login"))
            return jsonify({"ok": True})

        return self


def install_auth(app, ui_dir: str = "ui") -> ConsoleAuth:
    """Attach console auth to a Flask app. Call after routes are registered."""
    return ConsoleAuth(app, ui_dir=ui_dir).register()
