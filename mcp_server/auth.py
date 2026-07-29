"""Bearer-token validation and scope checks.

stdio transport: the process belongs to the local user; scopes come straight
from CFOP_MCP_SCOPES and there is no token exchange.

streamable-http transport: every request must carry the shared bearer token.
The single-token MVP means scopes are per-process (from env), not per-token;
multi-token support would move the scope lookup into the middleware.
"""

import hmac
import json


class ScopeError(Exception):
    """Raised when a tool call needs a scope the server was not granted."""


def require_scope(granted, needed):
    if needed not in granted:
        raise ScopeError(
            f"tool requires scope '{needed}'; this server grants {sorted(granted)}")


class BearerAuthMiddleware:
    """Pure-ASGI middleware rejecting HTTP requests without the shared token."""

    def __init__(self, app, token):
        self.app = app
        self._expected = f"Bearer {token}"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        auth = ""
        for name, value in scope.get("headers") or []:
            if name == b"authorization":
                auth = value.decode("latin-1")
                break
        if not hmac.compare_digest(auth, self._expected):
            body = json.dumps({
                "code": "unauthorized",
                "message": "missing or invalid bearer token",
                "retryable": False,
            }).encode()
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)
