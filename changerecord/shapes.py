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
    image_digest: str
    flag_snapshot: Dict[str, Any] = field(default_factory=dict)
    investigation_id: Any = None
    risk: str = ""
    confidence: Any = None


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
        return {"ref": self.id, "url": self.url}


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
        "executor_image_digest": intent.image_digest,
        "flag_snapshot": intent.flag_snapshot,
        "outcome": None,
    }


def intent_from_body(body: Dict[str, Any]) -> Intent:
    flags = body.get("flag_snapshot") or {}
    if not isinstance(flags, dict):
        flags = {"_value": flags}
    commands = body.get("commands") or []
    if not isinstance(commands, list):
        commands = [str(commands)]
    return Intent(
        remediation_id=body.get("remediation_id"),
        host=str(body.get("host") or ""),
        commands=[str(c) for c in commands],
        justification=str(body.get("justification") or ""),
        image_digest=str(body.get("image_digest") or ""),
        flag_snapshot=flags,
        investigation_id=body.get("investigation_id"),
        risk=str(body.get("risk") or ""),
        confidence=body.get("confidence"),
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
