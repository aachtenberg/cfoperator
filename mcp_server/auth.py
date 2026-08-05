"""Bearer-token validation and scope checks.

stdio transport: the process belongs to the local user; scopes come straight
from CFOP_MCP_SCOPES and there is no token exchange.

streamable-http transport: every request must carry a bearer token. Given an
AuthStore, the token is resolved against the database and its own scopes ride
along on the ASGI scope, so two clients hitting the same process no longer
share one grant. Without a store — or for a secret the database does not know —
the shared CFOP_MCP_TOKEN still passes, because the deployment that mints
per-token credentials and the one that rolls the sealed secret are not the same
day.
"""

import hmac
import json
import logging

logger = logging.getLogger(__name__)

# Where the resolved TokenIdentity is parked on the ASGI scope. Tool dispatch
# reads it for per-token scopes; the audit line reads its label to name which
# credential acted.
SCOPE_KEY = "cfop_token"

_legacy_identity_cache = None


class ScopeError(Exception):
    """Raised when a tool call needs a scope the server was not granted."""


def require_scope(granted, needed):
    if needed not in granted:
        raise ScopeError(
            f"tool requires scope '{needed}'; this server grants {sorted(granted)}")


def granted_scopes(asgi_scope, settings):
    """The scopes in force for one request.

    A database-backed token carries its own. Everything else — stdio, which has
    no ASGI scope at all, and the legacy shared token, which belongs to no user
    and so has no scopes of its own — falls back to the process's env grant.
    Promoting the shared token to a full-scope identity instead would widen it
    the moment the store was wired in.
    """
    identity = (asgi_scope or {}).get(SCOPE_KEY)
    if identity is not None and not identity.legacy:
        return identity.scopes
    return settings.scopes


def acting_token(asgi_scope):
    """The identity behind a request, for the audit line; None under stdio.

    Tool dispatch (mcp_server/tools/__init__.py) still audits against
    settings.scopes: reaching the ASGI scope from inside a FastMCP tool means
    threading the request context through every registration, which is a bigger
    change than this one. The identity is on the scope and ready for it.
    """
    return (asgi_scope or {}).get(SCOPE_KEY)


def _legacy_identity():
    """Stand-in identity for the shared token so the audit trail can name it.

    Imported lazily: a stdio deployment must not have to install SQLAlchemy
    just to compare an env scope list.
    """
    global _legacy_identity_cache
    if _legacy_identity_cache is None:
        from auth.store import TokenIdentity

        _legacy_identity_cache = TokenIdentity(
            token_id=None, label="legacy CFOP_MCP_TOKEN", legacy=True)
    return _legacy_identity_cache


async def _error(send, status, code, message, retryable, extra_headers=()):
    body = json.dumps({
        "code": code,
        "message": message,
        "retryable": retryable,
    }).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), *extra_headers],
    })
    await send({"type": "http.response.body", "body": body})


class BearerAuthMiddleware:
    """Pure-ASGI middleware rejecting HTTP requests without a usable token."""

    def __init__(self, app, token=None, store=None):
        self.app = app
        self.store = store
        self._expected = f"Bearer {token}" if token else None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        auth = ""
        for name, value in scope.get("headers") or []:
            if name == b"authorization":
                auth = value.decode("latin-1")
                break

        if self.store is not None and auth.startswith("Bearer "):
            try:
                identity = self.store.verify_token(auth[7:].strip())
            except Exception as exc:
                # A store that cannot be reached is an outage, not a denial.
                # 401 would read as "wrong token" and send the caller looking
                # for a credential problem; anything 2xx would be a fall-open.
                logger.error("token verification failed: %s", exc)
                await _error(send, 503, "auth_unavailable",
                             "could not verify bearer token", True)
                return
            if identity is not None:
                scope[SCOPE_KEY] = identity
                await self.app(scope, receive, send)
                return

        # Checked after the database so a real token is never shadowed by the
        # shared one, and warned about on every use so the migration finishes on
        # evidence rather than assumption.
        if self._expected and hmac.compare_digest(auth, self._expected):
            logger.warning(
                "request to %s authenticated with the deprecated shared CFOP_MCP_TOKEN",
                scope.get("path", "?"))
            self._record_legacy_use(scope)
            scope[SCOPE_KEY] = _legacy_identity()
            await self.app(scope, receive, send)
            return

        await _error(send, 401, "unauthorized",
                     "missing or invalid bearer token", False,
                     [(b"www-authenticate", b"Bearer")])

    def _record_legacy_use(self, scope):
        if self.store is None:
            return
        try:
            self.store.record_legacy_token_use(scope.get("path", "?"))
        except Exception as exc:  # pragma: no cover - auditing must not break auth
            logger.debug("could not record legacy token use: %s", exc)
