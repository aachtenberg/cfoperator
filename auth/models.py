"""SQLAlchemy models for console users, API tokens, and the auth audit trail.

These live in the same PostgreSQL database as the knowledge base and follow the
same conventions (declarative ``Base``, ``JSONB`` for structured columns,
timezone-aware timestamps). They are declared on their own ``Base`` so
``create_all`` here touches only the auth tables and never re-runs the much
larger knowledge-base schema.

Nothing secret is stored in plaintext: passwords are werkzeug hashes and API
tokens are kept only as a SHA-256 digest plus a short display prefix.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base

Base = declarative_base()

# JSONB in production, plain JSON everywhere else. The tests run against SQLite
# so they can exercise real queries — the last-admin guard and the token
# validity rules are logic worth testing against a database rather than a mock,
# and standing up PostgreSQL for that would mean they simply wouldn't run.
JSONColumn = JSON().with_variant(JSONB(), "postgresql")

# Console roles. Deliberately two: the console's dangerous surface is narrow
# (approve a remediation, mutate config, manage users), so a single "can do the
# dangerous things" bit is enough. Finer permissions belong in token scopes.
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
ROLES = (ROLE_ADMIN, ROLE_MEMBER)

# Token scopes, mirroring the MCP server's tiers: read ⊂ investigate ⊂
# remediate. Stored as an explicit list rather than a single tier so a token can
# be granted a non-contiguous set later without a migration, but
# ``expand_scopes`` below applies the tier implications on read.
SCOPE_READ = "read"
SCOPE_INVESTIGATE = "investigate"
SCOPE_REMEDIATE = "remediate"
SCOPES = (SCOPE_READ, SCOPE_INVESTIGATE, SCOPE_REMEDIATE)

# What each scope implies. Granting `remediate` without `read` would be a
# nonsense credential, so the implication is applied at verification time.
_SCOPE_IMPLIES = {
    SCOPE_READ: (SCOPE_READ,),
    SCOPE_INVESTIGATE: (SCOPE_INVESTIGATE, SCOPE_READ),
    SCOPE_REMEDIATE: (SCOPE_REMEDIATE, SCOPE_INVESTIGATE, SCOPE_READ),
}

# The scope ceiling a role may grant to a token it creates. A member must not be
# able to mint a token more powerful than the member themselves — otherwise
# token creation becomes a privilege-escalation path around the role check.
ROLE_SCOPE_CEILING = {
    ROLE_ADMIN: frozenset(SCOPES),
    ROLE_MEMBER: frozenset({SCOPE_READ, SCOPE_INVESTIGATE}),
}


def expand_scopes(scopes) -> frozenset:
    """Close a scope list over the tier hierarchy (``remediate`` ⇒ ``read``)."""
    granted = set()
    for scope in scopes or ():
        granted.update(_SCOPE_IMPLIES.get(scope, ()))
    return frozenset(granted)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: datetime | None) -> datetime | None:
    """Treat a stored timestamp as UTC if the driver returned it naive.

    PostgreSQL gives back timezone-aware values from a ``TIMESTAMPTZ`` column;
    SQLite, used by the tests, does not. Comparing the two kinds raises, so
    normalise before any comparison rather than at every call site.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


class User(Base):
    """A console login. One row per human."""

    __tablename__ = "auth_users"

    id = Column(Integer, primary_key=True)
    username = Column(String(255), nullable=False, unique=True)
    password_hash = Column(Text, nullable=False)
    role = Column(String(32), nullable=False, default=ROLE_MEMBER)
    # Soft-delete rather than DELETE: audit rows and tokens reference the user,
    # and "who approved this remediation last year" must stay answerable.
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_login_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_auth_users_username", "username"),
        Index("idx_auth_users_active", "is_active"),
    )

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def to_dict(self) -> dict:
        """Serialisation for API responses. Never includes the password hash."""
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }


class ApiToken(Base):
    """A bearer credential. The secret itself is never stored — only its digest.

    ``token_prefix`` holds the first few characters of the secret so the UI can
    show which token a row refers to without being able to reconstruct it.
    """

    __tablename__ = "auth_api_tokens"

    id = Column(Integer, primary_key=True)
    label = Column(String(255), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    token_prefix = Column(String(32), nullable=False)
    scopes = Column(JSONColumn, nullable=False, default=list)
    created_by = Column(Integer, ForeignKey("auth_users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # The hot path is "find the token with this digest", on every single
        # authenticated request.
        Index("idx_auth_tokens_hash", "token_hash"),
        Index("idx_auth_tokens_created_by", "created_by"),
    )

    def is_valid(self, now: datetime | None = None) -> bool:
        return self.status(now) == "active"

    def status(self, now: datetime | None = None) -> str:
        now = now or utcnow()
        if self.revoked_at is not None:
            return "revoked"
        expires = _as_aware(self.expires_at)
        if expires is not None and expires <= now:
            return "expired"
        return "active"

    def to_dict(self) -> dict:
        """Serialisation for API responses. Never includes the digest."""
        return {
            "id": self.id,
            "label": self.label,
            "token_prefix": self.token_prefix,
            "scopes": list(self.scopes or []),
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "status": self.status(),
        }


class AuthAudit(Base):
    """Append-only record of who authenticated and who changed what.

    Kept in the database rather than only in logs so the console can show it and
    so the legacy-token deprecation can be answered with a query ("has anything
    used the shared token this week?") instead of a log grep.
    """

    __tablename__ = "auth_audit"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    event = Column(String(64), nullable=False)
    # Free-form: the actor may be a user, a token, or nobody at all (a failed
    # login naming an account that does not exist).
    actor = Column(String(255), nullable=True)
    target = Column(String(255), nullable=True)
    source_ip = Column(String(64), nullable=True)
    detail = Column(JSONColumn, nullable=False, default=dict)

    __table_args__ = (
        Index("idx_auth_audit_created_at", "created_at"),
        Index("idx_auth_audit_event", "event"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "event": self.event,
            "actor": self.actor,
            "target": self.target,
            "source_ip": self.source_ip,
            "detail": dict(self.detail or {}),
        }


# Audit event names. Constants rather than inline strings so the route layer and
# the console query agree on spelling.
EVENT_LOGIN_OK = "login.success"
EVENT_LOGIN_FAIL = "login.failure"
EVENT_LOGIN_LOCKOUT = "login.lockout"
EVENT_USER_CREATED = "user.created"
EVENT_USER_UPDATED = "user.updated"
EVENT_USER_DEACTIVATED = "user.deactivated"
EVENT_PASSWORD_CHANGED = "user.password_changed"
EVENT_TOKEN_CREATED = "token.created"
EVENT_TOKEN_REVOKED = "token.revoked"
EVENT_TOKEN_AUTH_FAIL = "token.auth_failure"
EVENT_LEGACY_TOKEN_USED = "token.legacy_used"
