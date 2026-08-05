"""Multi-user auth for the agent console: users, roles, and API tokens.

Replaces the single sealed-secret credential set (``CFOP_UI_USERNAME`` /
``CFOP_UI_PASSWORD_HASH`` / ``CFOP_API_TOKEN``) with database-backed users and
individually revocable API tokens. The legacy env credentials still work during
the migration window — see ``bootstrap.py`` for how the first admin is seeded
and ``store.py`` for the deprecation accounting on the shared token.

The public surface is deliberately small so the console (``web_auth.py``), the
route blueprint (``routes.py``), and the MCP server can all build on the same
primitives:

* :class:`~auth.store.AuthStore` — every database operation, no Flask imports.
* :mod:`auth.tokens` — minting, hashing, and parsing of ``cfop_*`` secrets.
* :func:`~auth.bootstrap.bootstrap_admin` — idempotent first-admin seeding.
"""

from .models import ApiToken, AuthAudit, Base, User
from .store import AuthError, AuthStore, TokenIdentity
from .tokens import TOKEN_PREFIX, hash_token, mint_token, token_display_prefix

__all__ = [
    "ApiToken",
    "AuthAudit",
    "AuthError",
    "AuthStore",
    "Base",
    "TOKEN_PREFIX",
    "TokenIdentity",
    "User",
    "hash_token",
    "mint_token",
    "token_display_prefix",
]
