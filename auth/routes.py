"""The console's user, token, and audit management API.

Mounted at ``/api/auth``, behind the same gate as every other console route —
:mod:`web_auth` has already resolved the caller by the time a handler here runs,
so these routes ask *who is calling* through :func:`web_auth.current_user` and
never re-do credential work.

Two rules shape everything below:

* **A database failure is never a permission decision.** Every handler maps
  :class:`~auth.store.AuthError` (the caller got it wrong) to 400 and anything
  else (we got it wrong, or PostgreSQL is down) to 503. A 403 in the second case
  would send an operator hunting for a role problem that does not exist, and a
  200 would be falling open.
* **Members are scoped to themselves.** User management is admin-only via
  ``@require_role``; token routes are open to everyone but filtered by
  ``created_by``, because a member seeing or revoking another member's
  credentials is the same privilege escalation the role split exists to prevent.

Nothing here can emit a password hash or a token digest: both are excluded by
``to_dict`` on the models, and responses are built only from those dicts.
"""

from __future__ import annotations

import functools
import logging

from flask import Blueprint, g, jsonify, request

from auth.models import (
    EVENT_PASSWORD_CHANGED,
    EVENT_TOKEN_CREATED,
    EVENT_TOKEN_REVOKED,
    EVENT_USER_CREATED,
    EVENT_USER_DEACTIVATED,
    EVENT_USER_UPDATED,
    ROLE_MEMBER,
)
from auth.store import AuthError
from web_auth import ROLE_ADMIN, current_role, current_token, current_user, require_role

logger = logging.getLogger(__name__)

# An unbounded limit lets a single request pull the whole audit table into
# memory and into a browser; the console's table only ever shows a page.
_MAX_AUDIT_LIMIT = 1000
_DEFAULT_AUDIT_LIMIT = 100


def _guarded(fn):
    """Map store failures onto status codes without ever implying a denial."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except AuthError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logger.error("auth API %s %s failed: %s", request.method, request.path, exc,
                         exc_info=True)
            return jsonify({"error": "authentication backend unavailable"}), 503

    return wrapper


def _effective_role() -> str | None:
    """The caller's role, resolved exactly as ``require_role`` resolves it.

    Going through :class:`~web_auth.ConsoleAuth` rather than reading the session
    means a role decision made inside a handler cannot disagree with the one the
    decorator made on the way in — including in the ``CFOP_AUTH_DISABLED`` local
    mode, where there is no identity at all and everything is admin.
    """
    auth = getattr(g, "cfop_auth", None)
    if auth is None:
        return current_role()
    if auth.disabled:
        return ROLE_ADMIN
    return auth.effective_role()


def _caller_user_id():
    """The ``auth_users`` row behind this request, or None.

    None is a real answer, not an error: the legacy shared token and the
    environment-credential mode both authenticate without belonging to a user.
    """
    user = current_user()
    if user:
        return user.get("id")
    token = current_token()
    return token.user_id if token is not None else None


def _actor() -> str:
    """Who to name in the audit trail. A token acts under its label, not a
    username, so the two cannot be confused when reading the log back."""
    user = current_user()
    if user and user.get("username"):
        return user["username"]
    token = current_token()
    if token is not None:
        return f"token:{token.label}"
    return "unknown"


def build_auth_blueprint(store) -> Blueprint:
    """Build the ``/api/auth`` blueprint against an :class:`~auth.store.AuthStore`.

    Takes the store as an argument rather than importing a module-level
    singleton so the console can register the same routes against a test
    database, and so an app with no auth database simply never registers them.
    """
    bp = Blueprint("auth_api", __name__, url_prefix="/api/auth")

    def audit(event: str, target: str | None = None, **detail) -> None:
        store.record(event, actor=_actor(), target=target,
                     source_ip=request.remote_addr, **detail)

    def payload() -> dict:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    # ---- users --------------------------------------------------------

    @bp.get("/users")
    @require_role(ROLE_ADMIN)
    @_guarded
    def list_users():
        return jsonify({"users": store.list_users()})

    @bp.post("/users")
    @require_role(ROLE_ADMIN)
    @_guarded
    def create_user():
        data = payload()
        user = store.create_user(
            data.get("username") or "",
            data.get("password") or "",
            role=data.get("role") or ROLE_MEMBER,
            actor=_actor(),
        )
        audit(EVENT_USER_CREATED, target=user["username"], role=user["role"])
        return jsonify(user), 201

    @bp.patch("/users/<int:user_id>")
    @require_role(ROLE_ADMIN)
    @_guarded
    def update_user(user_id: int):
        data = payload()
        if "role" not in data and "is_active" not in data:
            raise AuthError("nothing to update: send role and/or is_active")

        is_active = data.get("is_active")
        # A string "false" is truthy, so an unvalidated value here would
        # silently *activate* the account an operator meant to switch off.
        if is_active is not None and not isinstance(is_active, bool):
            raise AuthError("is_active must be true or false")

        user = store.update_user(
            user_id, role=data.get("role"), is_active=is_active, actor=_actor(),
        )
        # Deactivation gets its own event: "who switched off whose account, and
        # when" is an incident question, and it would be unanswerable if it were
        # buried among ordinary role edits.
        event = EVENT_USER_DEACTIVATED if is_active is False else EVENT_USER_UPDATED
        audit(event, target=user["username"], role=user["role"], is_active=user["is_active"])
        return jsonify(user)

    @bp.post("/users/<int:user_id>/password")
    @require_role(ROLE_ADMIN)
    @_guarded
    def reset_password(user_id: int):
        """Admin reset, for the operator who has locked themselves out.

        Deliberately does not require the target's current password — an admin
        who could be asked for it would not need this route.
        """
        user = store.set_password(user_id, payload().get("password") or "", actor=_actor())
        audit(EVENT_PASSWORD_CHANGED, target=user["username"], reset_by_admin=True)
        return jsonify(user)

    # ---- self service -------------------------------------------------

    @bp.post("/password")
    @_guarded
    def change_own_password():
        user = current_user()
        if not user or user.get("id") is None:
            # A bearer token and the legacy environment credential both
            # authenticate without owning an ``auth_users`` row, so there is
            # nothing to change. Say so rather than dereferencing a None id and
            # returning a 503 that looks like an outage.
            return jsonify({
                "error": "this session is not tied to a user account",
                "detail": "log in with a console account to change a password",
            }), 409

        data = payload()
        new_password = data.get("new_password") or ""
        if not new_password:
            raise AuthError("new_password is required")

        # Re-proving the current password is what stops a stolen or borrowed
        # session from being turned into permanent account ownership.
        if store.verify_login(user["username"], data.get("current_password") or "") is None:
            logger.warning("password change refused for %s: wrong current password",
                           user["username"])
            return jsonify({"error": "current password is incorrect"}), 403

        store.set_password(user["id"], new_password, actor=user["username"])
        audit(EVENT_PASSWORD_CHANGED, target=user["username"], self_service=True)
        return jsonify({"ok": True})

    # ---- tokens -------------------------------------------------------

    @bp.get("/tokens")
    @_guarded
    def list_tokens():
        if _effective_role() == ROLE_ADMIN:
            return jsonify({"tokens": store.list_tokens()})

        user_id = _caller_user_id()
        if user_id is None:
            # No owner to filter on. An unfiltered list would hand every token
            # row in the deployment to the weakest credential there is.
            return jsonify({"tokens": []})
        return jsonify({"tokens": store.list_tokens(created_by=user_id)})

    @bp.post("/tokens")
    @_guarded
    def create_token():
        data = payload()
        scopes = data.get("scopes")
        if isinstance(scopes, str):
            # Iterating a string would grant a token one scope per character,
            # all of them unknown — a confusing 400 instead of a clear one.
            raise AuthError("scopes must be a list")

        expires_in_days = data.get("expires_in_days")
        if expires_in_days is not None:
            try:
                expires_in_days = int(expires_in_days)
            except (TypeError, ValueError):
                raise AuthError("expires_in_days must be a number")

        row, secret = store.create_token(
            data.get("label") or "",
            scopes or [],
            created_by=_caller_user_id(),
            # The store defaults this to admin. Passing the caller's real role —
            # and the *lower* ceiling when the role cannot be determined — is
            # what keeps a member from minting itself a remediate credential.
            creator_role=_effective_role() or ROLE_MEMBER,
            expires_in_days=expires_in_days,
        )
        audit(EVENT_TOKEN_CREATED, target=row["token_prefix"],
              token_id=row["id"], label=row["label"], scopes=row["scopes"])
        # The only moment the secret exists in cleartext anywhere: the store
        # keeps a SHA-256 digest and never the value, so a caller that loses
        # this response has to mint a new token.
        return jsonify({**row, "secret": secret}), 201

    @bp.delete("/tokens/<int:token_id>")
    @_guarded
    def revoke_token(token_id: int):
        if _effective_role() != ROLE_ADMIN:
            user_id = _caller_user_id()
            owned = (
                {t["id"] for t in store.list_tokens(created_by=user_id)}
                if user_id is not None else set()
            )
            if token_id not in owned:
                # 403 for "someone else's" and for "no such token" alike:
                # distinguishing them would turn this route into a way to
                # enumerate which token ids exist.
                return jsonify({
                    "error": "forbidden",
                    "detail": "a member may only revoke tokens they created",
                }), 403

        row = store.revoke_token(token_id, actor=_actor())
        audit(EVENT_TOKEN_REVOKED, target=row["token_prefix"],
              token_id=row["id"], label=row["label"])
        return jsonify(row)

    # ---- audit --------------------------------------------------------

    @bp.get("/audit")
    @require_role(ROLE_ADMIN)
    @_guarded
    def recent_audit():
        raw = request.args.get("limit", _DEFAULT_AUDIT_LIMIT)
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            raise AuthError("limit must be a number")
        limit = max(1, min(limit, _MAX_AUDIT_LIMIT))

        return jsonify({
            "audit": store.recent_audit(limit=limit, event=request.args.get("event") or None),
            "limit": limit,
        })

    return bp
