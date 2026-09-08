"""SSH identity and inventory config for a cockpit session (CFOP-146).

The image carries ``openssh-client``. Until this module existed, nothing gave
the session a key or a name→address map, so ``ssh raspberrypi5`` failed on a
host CFOperator is specifically responsible for observing.

The forensics key is the same one the deep-investigation worker and the
node-action executor already hold. It is copied into the *session* credential
(the per-investigation Secret, or the host session directory) so it dies with
the TTL, rather than mounting the standing ``cfop-forensics-ssh`` Secret into
every cockpit pod.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

logger = logging.getLogger("cfoperator.cockpit")

#: Filename of the generated OpenSSH config inside the staged ``~/.ssh``.
SSH_CONFIG_NAME = "config"

#: Container-tier env var. Docker ``--env-file`` cannot carry newlines, so the
#: staged files travel as one base64 JSON object. Visible to ``docker inspect``,
#: which is the same documented degradation the session token already accepts.
SSH_BUNDLE_ENV = "CFOP_COCKPIT_SSH_BUNDLE"

#: Secret / home files that are never a private key.
_SKIP_NAMES = frozenset({
    "CFOP_API_TOKEN", "known_hosts", "authorized_keys", "config",
    "config.d", "environment", "rc",
})

#: Names ssh will try as a default identity. A secret that uses Kubernetes'
#: ``ssh-privatekey`` convention is also copied to ``id_rsa`` so those defaults
#: hit.
_DEFAULT_IDENTITY_NAMES = ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")

_HOST_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9._-]+$")


def fleet_ssh_config(
    hosts: Mapping[str, Any],
    default_user: str = "sre",
    identity_files: Optional[Iterable[str]] = None,
) -> str:
    """OpenSSH config so ``ssh raspberrypi5`` hits ``infrastructure.hosts``.

    ``StrictHostKeyChecking no`` matches ``tools.ssh.SSHTools``: a cockpit is
    mid-incident and cannot prompt on a new host key. Identity files default to
    the staged ``~/.ssh`` names; host-tier sessions pass absolute paths in the
    session directory so the wrapper does not have to overwrite ``~/.ssh``.
    """
    id_files = [p for p in (identity_files or _default_identity_paths()) if p]
    lines = [
        "# Generated for a cfoperator cockpit session (CFOP-146).",
        "# Host aliases are the keys of infrastructure.hosts.",
        "Host *",
        "    StrictHostKeyChecking no",
        "    UserKnownHostsFile /dev/null",
        "    IdentitiesOnly yes",
    ]
    for path in id_files:
        lines.append(f"    IdentityFile {path}")
    for name, cfg in sorted((hosts or {}).items()):
        if not _HOST_NAME.match(str(name)):
            continue
        if not isinstance(cfg, dict):
            continue
        address = str(cfg.get("address") or "").strip()
        if not address or "\n" in address or "\r" in address:
            continue
        ssh = cfg.get("ssh") if isinstance(cfg.get("ssh"), dict) else {}
        user = _ssh_user(ssh.get("user"), default_user)
        lines += ["", f"Host {name}", f"    HostName {address}", f"    User {user}"]
        port = ssh.get("port")
        if port:
            try:
                lines.append(f"    Port {int(port)}")
            except (TypeError, ValueError):
                pass
    return "\n".join(lines) + "\n"


def identity_paths_from_hosts(
    hosts: Mapping[str, Any],
    fallback: str = "",
) -> List[str]:
    """The distinct ``ssh.key_path`` values the inventory already names.

    Homelab installs stage the forensics key at ``/root/.ssh/id_rsa`` and point
    every host at it; Helm mounts the same secret at ``ssh_secret_dir``. Either
    is enough. ``/keys/id_rsa`` in tests does not exist on disk, so a test
    without a real secret dir loads nothing — it must not pick up the
    developer's ``~/.ssh``.
    """
    paths: List[str] = []
    seen = set()
    for cfg in (hosts or {}).values():
        if not isinstance(cfg, dict):
            continue
        ssh = cfg.get("ssh") if isinstance(cfg.get("ssh"), dict) else {}
        raw = str(ssh.get("key_path") or "").strip()
        if raw and raw not in seen:
            seen.add(raw)
            paths.append(raw)
    fb = (fallback or "").strip()
    if fb and fb not in seen:
        paths.append(fb)
    return paths


def load_identity_files(
    secret_dir: str = "",
    extra_paths: Optional[Iterable[str]] = None,
) -> Dict[str, str]:
    """Private-key material the agent can already read, keyed by filename."""
    files: Dict[str, str] = {}
    source = Path(secret_dir) if secret_dir else None
    if source is not None and source.is_dir():
        for entry in source.iterdir():
            if entry.name.startswith(".") or not entry.is_file():
                continue
            if entry.name in _SKIP_NAMES or not _SAFE_FILE.match(entry.name):
                continue
            text = _read_text(entry)
            if text is not None:
                files[entry.name] = text
    for raw in extra_paths or ():
        path = Path(str(raw)).expanduser()
        if not path.is_file() or path.name in _SKIP_NAMES:
            continue
        if not _SAFE_FILE.match(path.name):
            continue
        if path.name in files:
            continue
        text = _read_text(path)
        if text is not None:
            files[path.name] = text
    if files and not any(name in files for name in _DEFAULT_IDENTITY_NAMES):
        # Kubernetes secrets often name the key ``ssh-privatekey``. ssh will
        # not try that as a default identity, so stage a copy under id_rsa.
        first = next(iter(files.values()))
        files.setdefault("id_rsa", first)
    return files


def session_ssh_files(
    hosts: Mapping[str, Any],
    default_user: str = "sre",
    secret_dir: str = "",
    extra_key_path: str = "",
    identity_dir: Optional[str] = None,
) -> Dict[str, str]:
    """Files to stage for a session: identities plus the generated config.

    Empty when the agent has no key. A config without a key would tell the
    model ``ssh raspberrypi5`` works when it does not.
    """
    extras = identity_paths_from_hosts(hosts, extra_key_path)
    files = load_identity_files(secret_dir, extras)
    if not files:
        return {}
    if identity_dir:
        id_files = [
            f"{identity_dir.rstrip('/')}/{name}"
            for name in files
            if name != SSH_CONFIG_NAME
        ]
    else:
        id_files = None
    files[SSH_CONFIG_NAME] = fleet_ssh_config(
        hosts, default_user, identity_files=id_files)
    return files


def encode_ssh_bundle(files: Mapping[str, str]) -> str:
    """Single-line encoding for a docker env-file."""
    payload = {name: body for name, body in files.items() if _SAFE_FILE.match(name)}
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode("ascii")


def decode_ssh_bundle(blob: str) -> Dict[str, str]:
    """Inverse of :func:`encode_ssh_bundle`. Unknown input is an empty dict."""
    try:
        raw = json.loads(base64.b64decode(blob))
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for name, body in raw.items():
        if _SAFE_FILE.match(str(name)) and isinstance(body, str):
            out[str(name)] = body
    return out


def _ssh_user(raw: Any, default_user: str) -> str:
    """A User value safe to write into ssh_config, or the known-safe ``sre``.

    Falling back to ``default_user`` after it failed ``_HOST_NAME`` would
    re-inject the value that just failed — whitespace or a newline becomes
    another directive.
    """
    for candidate in (raw, default_user, "sre"):
        user = str(candidate or "").strip()
        if _HOST_NAME.match(user):
            return user
    return "sre"


def _default_identity_paths() -> List[str]:
    return [f"~/.ssh/{name}" for name in _DEFAULT_IDENTITY_NAMES] + ["~/.ssh/ssh-privatekey"]


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("cockpit: skipping ssh identity %s (%s)", path, exc)
        return None
