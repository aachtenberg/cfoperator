"""Hosted-provider API keys: the one lookup (CFOP-111).

A key set from Admin → LLM lives in the knowledge base, encrypted, and wins
over the process env; the env stays the bootstrap and deploy path (sealed
secrets, Helm, compose). Every place the agent or the console needs a key
asks here -- provider availability, model listing, chat, the fallback
chain's skip-if-no-key, the judge's eligibility -- so none of them can
disagree about whether a provider is usable. tests/test_llm_keys_single_lookup.py
fails on a ``getenv`` of a key anywhere else.

At rest: ``agent_settings`` is plaintext, read by the agent and by
event_runtime, and the Postgres volume is backed up off the cluster, so a
value is never stored in the clear. It is Fernet-encrypted under a key
derived from CFOP_SESSION_SECRET, which every deployment already provisions
and the console refuses to sign sessions without. Rotate that secret and the
stored keys become unreadable: each reads as unset (one warning) and the env
takes over. Without the secret a key cannot be stored at all.

No module state beyond the warn-once set, on purpose: the agent imports this
bare (``import llm_keys``) and the console as ``agent.llm_keys`` -- two
module objects -- so a cache here would be two caches, one of them stale
for its TTL after a rotation. A read is one ``get_setting``, which a chain
walk already does several times over.
"""

import base64
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SETTING_PREFIX = 'llm_key_'
SECRET_ENV = 'CFOP_SESSION_SECRET'
ANTHROPIC_KEY_ENV = 'ANTHROPIC_API_KEY'
# Longest key any of the six vendors issues is well under this.
MAX_KEY_LENGTH = 512
_ENC_PREFIX = 'enc:v1:'
_HKDF_INFO = b'cfoperator llm_keys v1'
_warned_unreadable: set = set()
# One warning per DB outage, not per call: set on the first failed read,
# cleared by the next successful one.
_db_outage_warned = False


class KeysUnavailable(RuntimeError):
    """A key cannot be stored: nothing to encrypt it under."""


def registry() -> Dict[str, Dict[str, Any]]:
    """The agent's OpenAI-compatible provider registry, lazily -- a top-level
    import would be circular (see web_server._openai_compat_providers)."""
    from agent import OPENAI_COMPAT_PROVIDERS
    return OPENAI_COMPAT_PROVIDERS


def hosted_backends() -> List[str]:
    """Every backend that takes a key: the registry's, then Anthropic."""
    return [*registry().keys(), 'anthropic']


def key_env_for(backend: str) -> Optional[str]:
    """The env var a backend's key comes from, or None for one with no key."""
    if backend == 'anthropic':
        return ANTHROPIC_KEY_ENV
    cfg = registry().get(backend) or {}
    return cfg.get('key_env') or None


def label_for(backend: str) -> str:
    if backend == 'anthropic':
        return 'Anthropic'
    return str((registry().get(backend) or {}).get('label') or backend)


def _fernet():
    """A Fernet under the session secret, or None when the secret is unset.
    Raises ImportError when the cryptography library is missing."""
    secret = os.getenv(SECRET_ENV, '').strip()
    if not secret:
        return None
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=_HKDF_INFO).derive(secret.encode('utf-8'))
    return Fernet(base64.urlsafe_b64encode(raw))


def _stored(kb, backend: str) -> Optional[str]:
    """The raw stored value: '' when unset, None when the DB cannot be asked.

    ``strict=True`` so a ResilientKnowledgeBase that is marked unhealthy
    raises instead of handing back the default -- otherwise an outage reads
    as "no console key" and the env var takes over silently, which is the
    wrong answer when the env key is the one that was just rotated out. The
    env still answers during the outage (the process had nothing else before
    the console could set a key), but it says so, once per outage, and the
    console shows the source as unknown rather than env."""
    global _db_outage_warned
    if kb is None:
        return ''
    try:
        value = str(kb.get_setting(SETTING_PREFIX + backend, '', strict=True) or '')
    except Exception as e:
        if not _db_outage_warned:
            _db_outage_warned = True
            logger.warning(f"[LLM-KEYS] cannot read console-set provider keys ({e}); "
                           f"env vars apply until the database is back")
        return None
    if _db_outage_warned:
        _db_outage_warned = False
        logger.info("[LLM-KEYS] database readable again; console-set provider keys apply")
    return value


def _decrypt(backend: str, stored: str) -> Optional[str]:
    """The plaintext, or None when the stored value cannot be read."""
    if not stored.startswith(_ENC_PREFIX):
        reason = 'it is not in the encrypted format'
    else:
        try:
            f = _fernet()
            if f is None:
                reason = f'{SECRET_ENV} is unset'
            else:
                return f.decrypt(stored[len(_ENC_PREFIX):].encode('ascii')).decode('utf-8')
        except ImportError:
            reason = 'the cryptography library is not installed'
        except Exception:
            reason = f'it does not decrypt under the current {SECRET_ENV} (rotated?)'
    if backend not in _warned_unreadable:
        _warned_unreadable.add(backend)
        logger.warning(f"[LLM-KEYS] the console-set key for {backend} is unreadable: "
                       f"{reason}; using the env var instead")
    return None


def api_key(kb, backend: str) -> str:
    """The key to call ``backend`` with: console-set (DB) over env, '' when
    neither. The one lookup."""
    env = key_env_for(backend)
    if not env:
        return ''
    stored = _stored(kb, backend)
    if stored:
        value = _decrypt(backend, stored)
        if value:
            return value.strip()
    return os.getenv(env, '').strip()


def source(kb, backend: str) -> Optional[str]:
    """Where the key in effect comes from: 'console', 'env', 'unreadable'
    (stored but not decryptable, env in effect if set), 'unknown' (the DB
    cannot be read, env in effect if set), or None."""
    env = key_env_for(backend)
    if not env:
        return None
    stored = _stored(kb, backend)
    if stored is None:
        return 'unknown'
    if stored:
        return 'console' if _decrypt(backend, stored) else 'unreadable'
    return 'env' if os.getenv(env, '').strip() else None


def storage_available() -> bool:
    """Whether a key could be stored right now (secret present, library present)."""
    try:
        return _fernet() is not None
    except ImportError:
        return False


def status(kb, with_hint: bool = True) -> List[Dict[str, Any]]:
    """One row per hosted provider for the console. Never the value: a
    configured flag, the source, and -- for admins -- the last four
    characters as a hint, so a rotation can be told from a no-op. A key
    shorter than 12 characters gets no hint: four of eight is too much."""
    rows = []
    for backend in hosted_backends():
        src = source(kb, backend)
        key = api_key(kb, backend)
        rows.append({
            'id': backend,
            'label': label_for(backend),
            'key_env': key_env_for(backend),
            'configured': bool(key),
            'source': src,
            'hint': key[-4:] if with_hint and len(key) >= 12 else '',
        })
    return rows


def set_key(kb, backend: str, value: str) -> None:
    """Store a key, encrypted. ValueError on a bad request, KeysUnavailable
    when there is nothing to encrypt under; a failed write propagates."""
    if backend not in hosted_backends():
        raise ValueError(f'unknown provider: {backend}')
    value = str(value or '').strip()
    if not value:
        raise ValueError('blank means unchanged; clear the key to remove it')
    if len(value) > MAX_KEY_LENGTH:
        raise ValueError('key too long')
    try:
        f = _fernet()
    except ImportError:
        raise KeysUnavailable('the cryptography library is not installed; keys stay in the env') from None
    if f is None:
        raise KeysUnavailable(f'{SECRET_ENV} is unset on the agent; a key is never stored in the clear')
    token = f.encrypt(value.encode('utf-8')).decode('ascii')
    kb.set_setting(SETTING_PREFIX + backend, _ENC_PREFIX + token)
    _warned_unreadable.discard(backend)


def clear_key(kb, backend: str) -> None:
    """Drop the console override so the env takes over. '' is unset: the
    column is NOT NULL."""
    if backend not in hosted_backends():
        raise ValueError(f'unknown provider: {backend}')
    kb.set_setting(SETTING_PREFIX + backend, '')
    _warned_unreadable.discard(backend)
