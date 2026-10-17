"""Secrets the console stores in ``agent_settings``, sealed at rest.

``agent_settings`` is plaintext, read by the agent and the event runtime, and
the Postgres volume is backed up off the cluster, so a credential is never
stored there in the clear. It is Fernet-encrypted under a key derived from
``CFOP_SESSION_SECRET``, which every deployment already provisions and the
console refuses to sign sessions without. Rotate that secret and a sealed
value becomes unreadable: callers read it as unset and say so.

Two users so far: hosted-provider API keys (agent/llm_keys.py, CFOP-111) and
MCP connector headers (tools/mcp_connectors.py, CFOP-443). Each passes its own
HKDF ``info``, so one purpose's ciphertext does not open under another's key.

``cryptography`` is imported lazily, as everything in cfshared must: the
event runtime imports this package on a stdlib-only host.
"""

from __future__ import annotations

import base64
import os
from typing import Optional

SECRET_ENV = 'CFOP_SESSION_SECRET'
PREFIX = 'enc:v1:'


class SealUnavailable(RuntimeError):
    """Nothing to seal under: the secret is unset or the library is missing."""


def fernet(info: bytes):
    """A Fernet under the session secret for ``info``, or None when the secret
    is unset. Raises ImportError when the cryptography library is missing."""
    secret = os.getenv(SECRET_ENV, '').strip()
    if not secret:
        return None
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=info).derive(secret.encode('utf-8'))
    return Fernet(base64.urlsafe_b64encode(raw))


def available(info: bytes) -> bool:
    """Whether a value could be sealed right now."""
    try:
        return fernet(info) is not None
    except ImportError:
        return False


def seal(value: str, info: bytes) -> str:
    """``value`` sealed for storage. SealUnavailable when it cannot be."""
    try:
        f = fernet(info)
    except ImportError:
        raise SealUnavailable('the cryptography library is not installed') from None
    if f is None:
        raise SealUnavailable(f'{SECRET_ENV} is unset; a secret is never stored in the clear')
    return PREFIX + f.encrypt(value.encode('utf-8')).decode('ascii')


def unseal(stored: str, info: bytes) -> Optional[str]:
    """The plaintext, or None when ``stored`` cannot be opened (not sealed,
    no secret, no library, or sealed under a secret since rotated)."""
    if not isinstance(stored, str) or not stored.startswith(PREFIX):
        return None
    try:
        f = fernet(info)
        if f is None:
            return None
        return f.decrypt(stored[len(PREFIX):].encode('ascii')).decode('utf-8')
    except Exception:
        return None
