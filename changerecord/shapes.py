"""Shared shapes for the change-record HTTP contract.

Backend-agnostic: github / snow / jira images all speak these JSON shapes.
Stdlib only.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class Intent:
    """What the agent proposes to do on a host, plus evidence stamps."""

    remediation_id: Any
    host: str
    commands: List[str]
    justification: str
    image: str  # executor image ref (tag or digest; digest resolution is a follow-up)
    flag_snapshot: Dict[str, Any] = field(default_factory=dict)
    investigation_id: Any = None
    risk: str = ""
    confidence: Any = None
    # CFOP-442: set when a named person already approved the exact commands
    # before the record existed (an admin confirming a command in the console
    # chat). The record is then evidence of that approval, not a request for
    # one: a backend commits it as approved instead of opening a review.
    # None for every other intent, which keeps the merge-as-approval path.
    approval: Optional[Dict[str, Any]] = None


@dataclass
class Approval:
    """Named identity that authorized the change, with a timestamp."""

    identity: str
    timestamp: str
    state: str = "approved"

    def to_dict(self) -> Dict[str, Any]:
        return {"identity": self.identity, "timestamp": self.timestamp, "state": self.state}


@dataclass
class RecordRef:
    """Opaque handle returned by open; backends stash whatever they need in meta."""

    id: str
    url: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"ref": self.id, "url": self.url}
        # Echoed only for a pre-approved record (CFOP-442), so a client can
        # tell a recorder that honoured the approval from one too old to know
        # the field, which silently opened a review instead.
        if isinstance(self.meta.get("approval"), dict):
            out["approval"] = self.meta["approval"]
        return out


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def record_document(intent: Intent) -> Dict[str, Any]:
    return {
        "kind": "cfop-change-record",
        "opened_at": utc_now(),
        "remediation_id": intent.remediation_id,
        "investigation_id": intent.investigation_id,
        "host": intent.host,
        "commands": intent.commands,
        "justification": intent.justification,
        "risk": intent.risk,
        "confidence": intent.confidence,
        "executor_image": intent.image,
        "flag_snapshot": intent.flag_snapshot,
        "approval": intent.approval,
        "outcome": None,
    }


def approval_from_body(raw: Any) -> Optional[Dict[str, Any]]:
    """The pre-approval an /open body carries, or None (CFOP-442).

    A named identity is required: an approval with nobody behind it is the
    thing a change record exists to rule out, so a malformed one is an error
    rather than a silent fall-back to the review path. Absent is fine.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("approval must be an object")
    identity = str(raw.get("identity") or "").strip()
    if not identity:
        raise ValueError("approval.identity is required")
    out = {
        "identity": identity,
        "timestamp": str(raw.get("timestamp") or "").strip() or utc_now(),
        "state": str(raw.get("state") or "").strip() or "approved",
    }
    for key in ("via", "chat_session_id", "reason"):
        if raw.get(key) not in (None, ""):
            out[key] = raw[key]
    return out


def intent_from_body(body: Dict[str, Any]) -> Intent:
    flags = body.get("flag_snapshot") or {}
    if not isinstance(flags, dict):
        flags = {"_value": flags}
    commands = body.get("commands") or []
    if not isinstance(commands, list):
        commands = [str(commands)]
    # Prefer ``image``; accept legacy ``image_digest`` for older clients.
    image = body.get("image")
    if image is None or image == "":
        image = body.get("image_digest") or ""
    return Intent(
        remediation_id=body.get("remediation_id"),
        host=str(body.get("host") or ""),
        commands=[str(c) for c in commands],
        justification=str(body.get("justification") or ""),
        image=str(image),
        flag_snapshot=flags,
        investigation_id=body.get("investigation_id"),
        risk=str(body.get("risk") or ""),
        confidence=body.get("confidence"),
        approval=approval_from_body(body.get("approval")),
    )


def encode_ref(meta: Dict[str, Any]) -> str:
    """Pack backend meta into an opaque URL-safe token (stateless recorder)."""
    raw = json.dumps(meta, separators=(",", ":"), default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_ref(token: str) -> Dict[str, Any]:
    pad = "=" * (-len(token) % 4)
    raw = base64.urlsafe_b64decode(token + pad)
    meta = json.loads(raw.decode("utf-8"))
    if not isinstance(meta, dict):
        raise ValueError("ref must decode to an object")
    return meta
