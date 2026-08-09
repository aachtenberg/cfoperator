"""First-admin seeding from the legacy sealed-secret environment.

The console's credentials currently arrive as ``CFOP_UI_USERNAME`` and
``CFOP_UI_PASSWORD_HASH`` from a SealedSecret in k3s. If the auth tables shipped
empty, the first deploy carrying this code would have no way in: no user to log
in as, and user creation is itself an authenticated, admin-only operation.

So on every start, if there are no users at all, the legacy environment
credentials become the first admin. The hash is copied across verbatim — this
code never sees the password, and the operator's existing one keeps working.

Once a real admin exists the seeding is skipped, so removing the environment
variables later is a no-op rather than a lockout, and re-adding them cannot
resurrect or overwrite an account that was deliberately deactivated.
"""

from __future__ import annotations

import logging
import os

from .models import ROLE_ADMIN, User, utcnow
from .store import AuthStore, default_db_url, describe_db_target

logger = logging.getLogger(__name__)


def bootstrap_admin(store: AuthStore) -> bool:
    """Seed the first admin if the user table is empty. Returns True if seeded."""
    username = os.getenv("CFOP_UI_USERNAME", "").strip()
    password_hash = os.getenv("CFOP_UI_PASSWORD_HASH", "").strip()

    if store.count_users() > 0:
        return False

    if not (username and password_hash):
        logger.error(
            "No console users exist and CFOP_UI_USERNAME / CFOP_UI_PASSWORD_HASH are "
            "unset — nobody can log in. Set them to seed the first admin, or create "
            "a user with scripts/create_admin.py."
        )
        return False

    # Written directly rather than through create_user(), which would hash the
    # value again — what the environment holds is already a werkzeug hash.
    session = store._session()
    try:
        session.add(
            User(
                username=username,
                password_hash=password_hash,
                role=ROLE_ADMIN,
                is_active=True,
                created_at=utcnow(),
            )
        )
        session.commit()
    finally:
        session.close()

    logger.warning(
        "Seeded first admin %r from CFOP_UI_USERNAME/CFOP_UI_PASSWORD_HASH. "
        "Create per-person accounts and then remove these variables from the "
        "sealed secret — they are only a bootstrap path.",
        username,
    )
    return True


def init_auth_store(seed_admin: bool = True) -> AuthStore | None:
    """Build the store, create its tables, and seed the first admin.

    ``seed_admin`` is False for processes that only *verify* credentials — the
    MCP server, for one. Seeding is the console's job; a sibling deployment
    quietly creating an admin account would be a surprising side effect of
    starting a read path.

    Returns None when no auth database is configured or reachable, which puts
    the console into legacy single-user mode rather than taking it down. That is
    the deliberate rollout order: this code can ship before the database exists,
    and every existing deploy keeps working on its environment credentials until
    one does.

    It is *not* a fall-open: legacy mode still demands the environment
    credentials, and if those are missing too the console returns 503 on every
    non-exempt route.
    """
    if os.getenv("CFOP_AUTH_DB_DISABLED", "").lower() in ("1", "true", "yes"):
        logger.info("auth database explicitly disabled — using legacy console credentials")
        return None

    # Logged before connecting, and without credentials: when this fails, the
    # first question is always "which database did it even try?" — and the
    # answer is the whole diagnosis when the answer is localhost.
    target = describe_db_target(default_db_url())
    try:
        store = AuthStore()
        store.ensure_schema()
    except Exception as exc:
        logger.error(
            "Could not open the auth database at %s (%s) — falling back to legacy "
            "console credentials. Users and API tokens are unavailable until this "
            "is fixed. Set POSTGRES_HOST/PORT/DB/USER/PASSWORD, or CFOP_AUTH_DB_URL "
            "to point at it directly.",
            target, exc,
        )
        return None

    logger.info("auth database connected at %s", target)

    if seed_admin:
        try:
            bootstrap_admin(store)
        except Exception as exc:
            logger.error("first-admin bootstrap failed: %s", exc)

    return store
