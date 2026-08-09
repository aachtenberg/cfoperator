"""Every database operation behind console auth.

Deliberately free of Flask imports: the console (:mod:`web_auth`), the route
blueprint (:mod:`auth.routes`), and the MCP server all need these operations,
and only one of those three is a Flask request context.

Failure policy: methods raise rather than returning a falsy value when the
database is unreachable. Callers turn that into a 503. An exception that reads
as "no such user" would fail *open* the moment PostgreSQL blinked, which is the
one outcome this module exists to prevent.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote_plus, urlsplit

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool
from werkzeug.security import check_password_hash, generate_password_hash

from .models import (
    EVENT_LEGACY_TOKEN_USED,
    EVENT_TOKEN_AUTH_FAIL,
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_SCOPE_CEILING,
    ROLES,
    SCOPES,
    ApiToken,
    AuthAudit,
    Base,
    User,
    expand_scopes,
    utcnow,
)
from .tokens import hash_token, looks_like_token, mint_token, token_display_prefix

logger = logging.getLogger(__name__)

# A werkzeug hash of a value nobody knows, used to burn the same CPU on a
# missing username as on a real one. Without it, "no such user" returns
# measurably faster than "wrong password" and the login form becomes a username
# oracle.
_DUMMY_HASH = generate_password_hash("cfoperator-nonexistent-account")

# last_used_at is nice for spotting stale credentials, but writing it on every
# request would put a write on the hot path of every authenticated call. One
# write per token per minute is plenty to answer "is this still in use?".
_LAST_USED_THROTTLE_SECONDS = 60


class AuthError(Exception):
    """A caller-correctable problem: bad role, duplicate username, last admin."""


@dataclass(frozen=True)
class TokenIdentity:
    """The result of verifying a bearer token.

    ``user_id`` is None for the legacy shared token, which belongs to no user —
    that is precisely why it is being retired.
    """

    token_id: Optional[int]
    label: str
    scopes: frozenset = field(default_factory=frozenset)
    user_id: Optional[int] = None
    legacy: bool = False

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


class AuthStore:
    """Owns the auth tables and every query against them."""

    def __init__(self, db_url: Optional[str] = None, engine=None):
        if engine is not None:
            self.engine = engine
        else:
            self.engine = create_engine(
                db_url or default_db_url(),
                poolclass=QueuePool,
                pool_size=5,
                max_overflow=10,
                pool_pre_ping=True,
            )
        self.Session = sessionmaker(bind=self.engine)
        self._schema_ready = False
        self._last_used_cache: dict[int, float] = {}
        self._lock = threading.Lock()

    # ---- schema -------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create the auth tables if absent. Idempotent; safe on every start."""
        if self._schema_ready:
            return
        Base.metadata.create_all(self.engine)
        self._schema_ready = True
        logger.info("auth schema ready")

    def _session(self):
        return self.Session()

    # ---- users --------------------------------------------------------

    def create_user(
        self,
        username: str,
        password: str,
        role: str = ROLE_MEMBER,
        actor: Optional[str] = None,
    ) -> dict:
        username = (username or "").strip()
        if not username:
            raise AuthError("username is required")
        if not password:
            raise AuthError("password is required")
        if role not in ROLES:
            raise AuthError(f"role must be one of {list(ROLES)}")

        session = self._session()
        try:
            existing = session.query(User).filter(func.lower(User.username) == username.lower()).first()
            if existing is not None:
                raise AuthError(f"user {username!r} already exists")
            user = User(
                username=username,
                password_hash=generate_password_hash(password),
                role=role,
                is_active=True,
            )
            session.add(user)
            session.commit()
            logger.info("created user %s (role=%s) by %s", username, role, actor or "system")
            return user.to_dict()
        finally:
            session.close()

    def list_users(self) -> list[dict]:
        session = self._session()
        try:
            rows = session.query(User).order_by(User.username).all()
            return [u.to_dict() for u in rows]
        finally:
            session.close()

    def get_user(self, user_id: int) -> Optional[dict]:
        session = self._session()
        try:
            user = session.get(User, user_id)
            return user.to_dict() if user else None
        finally:
            session.close()

    def get_user_by_username(self, username: str) -> Optional[dict]:
        session = self._session()
        try:
            user = (
                session.query(User)
                .filter(func.lower(User.username) == (username or "").strip().lower())
                .first()
            )
            return user.to_dict() if user else None
        finally:
            session.close()

    def update_user(
        self,
        user_id: int,
        role: Optional[str] = None,
        is_active: Optional[bool] = None,
        actor: Optional[str] = None,
    ) -> dict:
        if role is not None and role not in ROLES:
            raise AuthError(f"role must be one of {list(ROLES)}")

        session = self._session()
        try:
            user = session.get(User, user_id)
            if user is None:
                raise AuthError("no such user")

            # Losing the last admin means losing the ability to manage users at
            # all — recoverable only by re-seeding from env. Refuse both routes
            # to it (demotion and deactivation) rather than explaining the
            # recovery procedure afterwards.
            demoting = role is not None and role != ROLE_ADMIN and user.role == ROLE_ADMIN
            deactivating = is_active is False and user.is_active
            if (demoting or deactivating) and self._admin_count(session, exclude_id=user.id) == 0:
                raise AuthError("cannot remove the last active admin")

            if role is not None:
                user.role = role
            if is_active is not None:
                user.is_active = is_active
            session.commit()
            logger.info(
                "updated user %s (role=%s active=%s) by %s",
                user.username, user.role, user.is_active, actor or "system",
            )
            return user.to_dict()
        finally:
            session.close()

    def set_password(self, user_id: int, password: str, actor: Optional[str] = None) -> dict:
        if not password:
            raise AuthError("password is required")
        session = self._session()
        try:
            user = session.get(User, user_id)
            if user is None:
                raise AuthError("no such user")
            user.password_hash = generate_password_hash(password)
            session.commit()
            logger.info("password changed for %s by %s", user.username, actor or "system")
            return user.to_dict()
        finally:
            session.close()

    def count_users(self) -> int:
        session = self._session()
        try:
            return session.query(func.count(User.id)).scalar() or 0
        finally:
            session.close()

    def _admin_count(self, session, exclude_id: Optional[int] = None) -> int:
        query = session.query(func.count(User.id)).filter(
            User.role == ROLE_ADMIN, User.is_active.is_(True)
        )
        if exclude_id is not None:
            query = query.filter(User.id != exclude_id)
        return query.scalar() or 0

    # ---- login --------------------------------------------------------

    def verify_login(self, username: str, password: str) -> Optional[dict]:
        """Return the user on success, None on any failure.

        One generic outcome for every failure mode — unknown user, wrong
        password, deactivated account — so the response cannot be used to
        enumerate accounts. The dummy hash keeps the timing flat too.
        """
        session = self._session()
        try:
            user = (
                session.query(User)
                .filter(func.lower(User.username) == (username or "").strip().lower())
                .first()
            )
            if user is None:
                check_password_hash(_DUMMY_HASH, password or "")
                return None
            if not check_password_hash(user.password_hash, password or ""):
                return None
            if not user.is_active:
                logger.warning("login refused for deactivated account %s", user.username)
                return None

            user.last_login_at = utcnow()
            result = user.to_dict()
            session.commit()
            return result
        finally:
            session.close()

    # ---- tokens -------------------------------------------------------

    def create_token(
        self,
        label: str,
        scopes: list,
        created_by: Optional[int] = None,
        creator_role: str = ROLE_ADMIN,
        expires_in_days: Optional[int] = None,
    ) -> tuple[dict, str]:
        """Mint a token. Returns ``(row, secret)`` — the secret is never recoverable."""
        label = (label or "").strip()
        if not label:
            raise AuthError("label is required")

        scopes = list(dict.fromkeys(scopes or []))
        if not scopes:
            raise AuthError("at least one scope is required")
        unknown = [s for s in scopes if s not in SCOPES]
        if unknown:
            raise AuthError(f"unknown scopes: {unknown}")

        # A member minting a `remediate` token would hand itself the one
        # capability its role withholds.
        ceiling = ROLE_SCOPE_CEILING.get(creator_role, frozenset())
        over = [s for s in scopes if s not in ceiling]
        if over:
            raise AuthError(f"role {creator_role!r} cannot grant scopes: {over}")

        secret = mint_token()
        expires_at = None
        if expires_in_days is not None:
            if expires_in_days <= 0:
                raise AuthError("expires_in_days must be positive")
            expires_at = utcnow() + timedelta(days=expires_in_days)

        session = self._session()
        try:
            token = ApiToken(
                label=label,
                token_hash=hash_token(secret),
                token_prefix=token_display_prefix(secret),
                scopes=scopes,
                created_by=created_by,
                expires_at=expires_at,
            )
            session.add(token)
            session.commit()
            logger.info("minted token %s (%s) scopes=%s", token.token_prefix, label, scopes)
            return token.to_dict(), secret
        finally:
            session.close()

    def list_tokens(self, created_by: Optional[int] = None) -> list[dict]:
        """All tokens, or only those belonging to one user."""
        session = self._session()
        try:
            query = session.query(ApiToken)
            if created_by is not None:
                query = query.filter(ApiToken.created_by == created_by)
            rows = query.order_by(ApiToken.created_at.desc()).all()
            return [t.to_dict() for t in rows]
        finally:
            session.close()

    def revoke_token(self, token_id: int, actor: Optional[str] = None) -> dict:
        session = self._session()
        try:
            token = session.get(ApiToken, token_id)
            if token is None:
                raise AuthError("no such token")
            if token.revoked_at is None:
                token.revoked_at = utcnow()
                session.commit()
                logger.info("revoked token %s by %s", token.token_prefix, actor or "system")
            return token.to_dict()
        finally:
            session.close()

    def verify_token(self, presented: str) -> Optional[TokenIdentity]:
        """Resolve a bearer secret to an identity, or None if it is not valid.

        Lookup is by digest, so the comparison is an indexed equality on a
        SHA-256 hex string rather than a scan — there is no secret-dependent
        branch for an attacker to time.
        """
        if not looks_like_token(presented or ""):
            return None

        digest = hash_token(presented)
        session = self._session()
        try:
            token = session.query(ApiToken).filter(ApiToken.token_hash == digest).first()
            if token is None:
                return None
            if not token.is_valid():
                logger.warning("rejected %s token %s", token.status(), token.token_prefix)
                return None

            # A deactivated human must not keep authenticating through a token
            # they minted while active.
            if token.created_by is not None:
                owner = session.get(User, token.created_by)
                if owner is not None and not owner.is_active:
                    logger.warning(
                        "rejected token %s: owner %s is deactivated",
                        token.token_prefix, owner.username,
                    )
                    return None

            identity = TokenIdentity(
                token_id=token.id,
                label=token.label,
                scopes=expand_scopes(token.scopes),
                user_id=token.created_by,
            )
            self._touch_last_used(session, token)
            return identity
        finally:
            session.close()

    def _touch_last_used(self, session, token: ApiToken) -> None:
        now = time.time()
        with self._lock:
            last = self._last_used_cache.get(token.id, 0.0)
            if now - last < _LAST_USED_THROTTLE_SECONDS:
                return
            self._last_used_cache[token.id] = now
        try:
            token.last_used_at = utcnow()
            session.commit()
        except Exception as exc:  # pragma: no cover - bookkeeping must never 500
            session.rollback()
            logger.debug("could not update last_used_at for %s: %s", token.token_prefix, exc)

    # ---- audit --------------------------------------------------------

    def record(
        self,
        event: str,
        actor: Optional[str] = None,
        target: Optional[str] = None,
        source_ip: Optional[str] = None,
        **detail,
    ) -> None:
        """Append an audit row. Never raises — auditing must not break auth."""
        session = self._session()
        try:
            session.add(
                AuthAudit(
                    event=event,
                    actor=actor,
                    target=target,
                    source_ip=source_ip,
                    detail=detail or {},
                )
            )
            session.commit()
        except Exception as exc:
            session.rollback()
            logger.warning("could not write audit event %s: %s", event, exc)
        finally:
            session.close()

    def recent_audit(self, limit: int = 100, event: Optional[str] = None) -> list[dict]:
        session = self._session()
        try:
            query = session.query(AuthAudit)
            if event:
                query = query.filter(AuthAudit.event == event)
            rows = query.order_by(AuthAudit.created_at.desc()).limit(limit).all()
            return [r.to_dict() for r in rows]
        finally:
            session.close()

    def record_token_failure(self, source_ip: Optional[str] = None) -> None:
        self.record(EVENT_TOKEN_AUTH_FAIL, source_ip=source_ip)

    def record_legacy_token_use(self, path: str, source_ip: Optional[str] = None) -> None:
        """Track shared-token usage so it can be retired on evidence, not hope."""
        self.record(EVENT_LEGACY_TOKEN_USED, source_ip=source_ip, path=path)


def _first_env(*names: str, default: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


def default_db_url() -> str:
    """Build the DSN from the database variables the deployment already sets.

    ``POSTGRES_*`` first, because that is what the k3s manifests and
    docker-compose actually provide and what agent.py builds its own connection
    from. ``KNOWLEDGE_BASE_PG_*`` is accepted as a fallback because
    knowledge_base.py reads those directly, so a deployment configured that way
    should work too.

    Reading only the KNOWLEDGE_BASE_* family was the original bug: the pod sets
    POSTGRES_*, so every lookup missed, the defaults took over, and auth spent a
    release quietly connecting to localhost and falling back to legacy
    single-user mode.

    The defaults match agent.py's, so an unconfigured deployment aims at the
    in-cluster service rather than at localhost, where nothing is listening.
    """
    explicit = os.getenv("CFOP_AUTH_DB_URL", "").strip()
    if explicit:
        return explicit

    host = _first_env("POSTGRES_HOST", "KNOWLEDGE_BASE_PG_HOST", default="postgres")
    port = _first_env("POSTGRES_PORT", "KNOWLEDGE_BASE_PG_PORT", default="5432")
    database = _first_env("POSTGRES_DB", "KNOWLEDGE_BASE_PG_DATABASE", default="cfoperator")
    user = _first_env("POSTGRES_USER", "KNOWLEDGE_BASE_PG_USER", default="cfoperator")
    password = _first_env("POSTGRES_PASSWORD", "KNOWLEDGE_BASE_PG_PASSWORD", default="")

    # Sealed-secret passwords routinely contain @ / : # — unescaped, those turn
    # into DSN syntax and surface as a confusing parse error or a connection to
    # the wrong host.
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{database}"
    )


def describe_db_target(url: str) -> str:
    """host:port/database from a DSN, for logging. Never includes credentials."""
    try:
        parsed = urlsplit(url)
        return f"{parsed.hostname}:{parsed.port or 5432}{parsed.path}"
    except Exception:
        return "unparseable DSN"
