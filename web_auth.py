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
  ``Authorization: Bearer <token>``. They are not interactive and must never be
  redirected to a login page.

Where credentials come from
---------------------------
When an :class:`~auth.store.AuthStore` is supplied, both mechanisms resolve
against the database: logins against ``auth_users``, bearer tokens against
``auth_api_tokens``. Roles and per-token scopes come from those rows.

Without a store — local docker-compose, and any deploy that has not yet been
given a database — the module falls back to the original single-credential
environment variables. The shared ``CFOP_API_TOKEN`` also stays honoured
*alongside* the database during the migration window, logging a deprecation
warning and an audit row on each use so it can be retired once the audit trail
shows nothing is using it.

Exempt paths
------------
``/api/health`` (kubelet probes) and ``/metrics`` (Prometheus scrape) stay open.
Both are read-only and non-sensitive, and gating them would break liveness
checks and monitoring for no benefit.

Failure mode is CLOSED
----------------------
If the console is configured for auth but the credentials are missing, every
non-exempt route returns 503 rather than falling open. The same applies when the
database is configured but unreachable: a lookup that cannot run is an error,
never an implicit "no such user". The deploy order is secrets first, then image,
so this window should not occur — but "misconfigured" must never silently mean
"unprotected".

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
    g,
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

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"


class AuthBackendUnavailable(Exception):
    """The credential store could not be reached to answer a check.

    Distinct from "this caller may not do that": the first is a 503 the caller
    should retry, the second is a 401/403 they should not.
    """


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


# ---- request-scoped identity ------------------------------------------
#
# Populated by the gate so route handlers and the require_role decorator can ask
# who is calling without repeating the credential work.


def current_user() -> dict | None:
    """The logged-in user for this request, or None for a token/anonymous call."""
    return getattr(g, "cfop_user", None)


def current_token():
    """The :class:`~auth.store.TokenIdentity` for this request, if any."""
    return getattr(g, "cfop_token", None)


def current_role() -> str | None:
    """Effective role: the user's role, or the role a token's scopes imply."""
    user = current_user()
    if user:
        return user.get("role")
    token = current_token()
    if token is not None:
        # A token is not a person and has no role. Treat `remediate` as
        # admin-equivalent because that is exactly the capability the admin role
        # gates; anything less is a member.
        return ROLE_ADMIN if token.has_scope("remediate") else ROLE_MEMBER
    return None


def require_role(role: str):
    """Gate a route on a role. Denies by default when identity is unknown.

    The role is re-read from the database rather than taken from the session
    cookie: a cookie issued to an admin stays valid for its whole lifetime, so
    trusting it would let a just-demoted account keep approving remediations
    until it happened to log out.
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            auth = getattr(g, "cfop_auth", None)
            if auth is not None and auth.disabled:
                return fn(*args, **kwargs)

            try:
                effective = auth.effective_role() if auth is not None else current_role()
            except AuthBackendUnavailable:
                # Same rule as the gate itself: a check that could not run is an
                # outage, not a denial. 401 here would send an admin looking for
                # a credential problem they do not have.
                return jsonify({"error": "authentication backend unavailable"}), 503

            if effective is None:
                return jsonify({"error": "authentication required"}), 401
            if role == ROLE_ADMIN and effective != ROLE_ADMIN:
                return jsonify({
                    "error": "forbidden",
                    "detail": f"this action requires the {role} role",
                }), 403
            return fn(*args, **kwargs)

        return wrapper

    return decorator


class ConsoleAuth:
    """Holds the configured credentials and installs the Flask hooks."""

    def __init__(self, app, ui_dir: str = "ui", store=None):
        self.app = app
        self.ui_dir = ui_dir
        self.store = store
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
                "Console auth is enabled but unconfigured (need an auth database, or "
                "CFOP_UI_USERNAME, CFOP_UI_PASSWORD_HASH, CFOP_API_TOKEN) — all "
                "non-exempt routes will return 503 until these are set."
            )
        elif self.store is not None:
            logger.info("Console auth active (database-backed users and API tokens)")
            if self.api_token:
                logger.warning(
                    "CFOP_API_TOKEN is still set. It remains valid for now, but it is "
                    "a shared credential that belongs to no user and cannot be revoked "
                    "individually — mint per-service tokens and remove it."
                )
        else:
            logger.info(
                "Console auth active in legacy single-user mode (user=%s) — no auth "
                "database configured", self.username,
            )

    @property
    def configured(self) -> bool:
        if self.store is not None:
            return True
        return bool(self.username and self.password_hash and self.api_token)

    @property
    def legacy_mode(self) -> bool:
        """True when credentials come from the environment, not the database."""
        return self.store is None

    # ---- identity -----------------------------------------------------

    def effective_role(self) -> str | None:
        """The caller's role, or None if there isn't one.

        Raises :class:`AuthBackendUnavailable` if the store cannot be reached,
        so an outage stays distinguishable from a caller who simply lacks the
        role. Returning None on a failed lookup would report a database problem
        as "you are not logged in".
        """
        if self.legacy_mode:
            # The single environment-configured operator is the only account
            # that exists, and it has always been able to do everything.
            if session.get("cfop_user") or current_token() is not None:
                return ROLE_ADMIN
            return None

        # check_request has already looked this user up and confirmed they are
        # active. Reusing that result keeps the role check to one query per
        # request, and closes the window where the gate admits a request whose
        # role check then fails against a database that went away in between.
        user = current_user()
        if user is not None:
            return user.get("role")

        user_id = session.get("cfop_user_id")
        if user_id is not None:
            try:
                user = self.store.get_user(user_id)
            except Exception as exc:
                logger.error("role lookup failed for user %s: %s", user_id, exc)
                raise AuthBackendUnavailable(str(exc)) from exc
            if not user or not user.get("is_active"):
                return None
            return user.get("role")

        return current_role()

    # ---- request gate -------------------------------------------------

    def _service_token_ok(self) -> bool:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        presented = header[7:].strip()
        if not presented:
            return False

        if self.store is not None:
            identity = self.store.verify_token(presented)
            if identity is not None:
                g.cfop_token = identity
                return True

        # Legacy shared token. Checked after the database so a real token is
        # never shadowed by it, and audited on every use so the migration can be
        # completed on evidence rather than assumption.
        if self.api_token and secrets.compare_digest(presented, self.api_token):
            logger.warning(
                "request to %s authenticated with the deprecated shared CFOP_API_TOKEN",
                request.path,
            )
            if self.store is not None:
                self.store.record_legacy_token_use(request.path, source_ip=_client_ip())
            g.cfop_token = _legacy_token_identity()
            return True

        return False

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
        g.cfop_auth = self
        if self.disabled:
            return None

        path = request.path
        if path in EXEMPT_PATHS:
            return None

        if not self.configured:
            return jsonify({
                "error": "console auth is not configured",
                "detail": "no auth database, and CFOP_UI_USERNAME / "
                          "CFOP_UI_PASSWORD_HASH / CFOP_API_TOKEN are unset",
            }), 503

        # A credential store that cannot be reached is an error, not a denial:
        # answering 401 would be indistinguishable from a wrong password, and
        # answering anything else would be falling open.
        try:
            if self._service_token_ok():
                return None
        except Exception as exc:
            logger.error("token verification failed: %s", exc)
            return jsonify({
                "error": "authentication backend unavailable",
            }), 503

        if self._completion_token_ok():
            return None

        if session.get("cfop_user"):
            if self.legacy_mode:
                return None
            # Re-check the account on every request so deactivating a user takes
            # effect immediately rather than whenever their cookie expires.
            try:
                user = self.store.get_user(session.get("cfop_user_id"))
            except Exception as exc:
                logger.error("session user lookup failed: %s", exc)
                return jsonify({"error": "authentication backend unavailable"}), 503
            if user and user.get("is_active"):
                g.cfop_user = user
                return None
            session.clear()

        if _wants_html():
            return redirect(url_for("login", next=request.full_path))
        return jsonify({"error": "authentication required"}), 401

    # ---- login --------------------------------------------------------

    def _verify_credentials(self, user: str, password: str):
        """Return the authenticated user dict, or None.

        Both factors are always compared before answering, and every failure
        yields the same generic error, so a wrong username is indistinguishable
        from a wrong password.
        """
        if self.store is not None:
            return self.store.verify_login(user, password)

        user_ok = secrets.compare_digest(user, self.username)
        pass_ok = check_password_hash(self.password_hash, password)
        if user_ok and pass_ok:
            return {"id": None, "username": self.username, "role": ROLE_ADMIN, "is_active": True}
        return None

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
                self._audit("login.lockout", source_ip=ip)
                return jsonify({"error": "too many attempts, try again later"}), 429

            if not self.configured:
                return jsonify({"error": "console auth is not configured"}), 503

            data = request.get_json(silent=True) or request.form
            user = (data.get("username") or "").strip()
            password = data.get("password") or ""

            try:
                authenticated = self._verify_credentials(user, password)
            except Exception as exc:
                logger.error("login verification failed: %s", exc)
                return jsonify({"error": "authentication backend unavailable"}), 503

            if not authenticated:
                _record_failure(ip)
                logger.warning("failed console login from %s (user=%r)", ip, user)
                self._audit("login.failure", actor=user, source_ip=ip)
                return jsonify({"error": "invalid credentials"}), 401

            _failures.pop(ip, None)
            session.clear()
            session["cfop_user"] = authenticated["username"]
            session["cfop_user_id"] = authenticated.get("id")
            session["cfop_role"] = authenticated.get("role", ROLE_ADMIN)
            session.permanent = True
            logger.info("console login from %s (user=%s)", ip, authenticated["username"])
            self._audit("login.success", actor=authenticated["username"], source_ip=ip)
            return jsonify({
                "ok": True,
                "next": data.get("next") or "/",
                "user": {
                    "username": authenticated["username"],
                    "role": authenticated.get("role", ROLE_ADMIN),
                },
            })

        @app.route("/logout", methods=["GET", "POST"])
        def logout():
            session.clear()
            if _wants_html():
                return redirect(url_for("login"))
            return jsonify({"ok": True})

        @app.route("/api/auth/me")
        def whoami():
            """Who am I and what may I do — the UI uses this to hide controls
            the caller cannot use. Not a security boundary: the server still
            enforces every one of them."""
            if self.disabled:
                return jsonify({"username": "dev", "role": ROLE_ADMIN, "auth_disabled": True})
            user = current_user()
            token = current_token()
            if user:
                return jsonify({**user, "auth_disabled": False})
            if token is not None:
                return jsonify({
                    "username": None,
                    "token_label": token.label,
                    "role": current_role(),
                    "scopes": sorted(token.scopes),
                    "auth_disabled": False,
                })
            return jsonify({"username": session.get("cfop_user"), "role": session.get("cfop_role")})

        return self

    def _audit(self, event: str, **kwargs) -> None:
        if self.store is None:
            return
        try:
            self.store.record(event, **kwargs)
        except Exception as exc:  # pragma: no cover - auditing must not break login
            logger.warning("could not record audit event %s: %s", event, exc)


def _legacy_token_identity():
    """A stand-in identity for the shared token, granting every scope.

    It has no user and no token id, which is what makes it visible as legacy in
    the audit trail — and what makes it worth removing.
    """
    from auth.store import TokenIdentity

    return TokenIdentity(
        token_id=None,
        label="legacy CFOP_API_TOKEN",
        scopes=frozenset({"read", "investigate", "remediate"}),
        user_id=None,
        legacy=True,
    )


def install_auth(app, ui_dir: str = "ui", store=None) -> ConsoleAuth:
    """Attach console auth to a Flask app. Call after routes are registered."""
    return ConsoleAuth(app, ui_dir=ui_dir, store=store).register()
