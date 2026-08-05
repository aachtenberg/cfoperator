"""Minting and hashing of ``cfop_*`` API tokens.

Token shape is ``cfop_<43 url-safe chars>`` — a recognisable prefix so a leaked
secret can be identified in a log or a paste, and 256 bits of entropy from
:func:`secrets.token_urlsafe`.

Why SHA-256 and not a password hash: these secrets are full-entropy random, so
there is nothing to brute-force, and verification happens on *every*
authenticated request. A deliberately slow KDF here would put ~100ms on the hot
path to defend against an attack that cannot succeed. Passwords, which are
low-entropy and human-chosen, still use werkzeug's slow hash — see
:mod:`auth.store`.
"""

from __future__ import annotations

import hashlib
import secrets

TOKEN_PREFIX = "cfop_"

# Bytes of randomness; token_urlsafe(32) yields a 43-character secret.
_TOKEN_BYTES = 32

# How much of the token to store for display. Long enough to disambiguate rows
# in the UI, far too short to be useful to anyone who reads it.
_DISPLAY_CHARS = 8


def mint_token() -> str:
    """Return a fresh token secret. This is the only time it exists in cleartext."""
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"


def hash_token(token: str) -> str:
    """Digest a token secret for storage and lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_display_prefix(token: str) -> str:
    """The non-secret fragment shown in the UI, e.g. ``cfop_A1b2C3d4``."""
    return token[: len(TOKEN_PREFIX) + _DISPLAY_CHARS]


def looks_like_token(value: str) -> bool:
    """Cheap shape check so a legacy shared token isn't hashed and looked up.

    Not a security boundary — it only avoids a pointless database round trip
    for credentials that cannot possibly be in the table.
    """
    return bool(value) and value.startswith(TOKEN_PREFIX)
