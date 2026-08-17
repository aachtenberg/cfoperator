#!/usr/bin/env python3
"""One-shot first-boot seeding for the docker-compose trial (CFOP-25).

A fresh database dead-ends the trial twice over: auth is DB-backed and
fail-closed, so the login page has no credentials that could open it, and
event_runtime authenticates to the agent with a DB-backed API token that
cannot exist yet. This script runs once as the compose `bootstrap` service,
before agent and event-runtime start, and makes both problems disappear:

  1. waits for Postgres and creates the auth schema;
  2. best-effort `CREATE EXTENSION vector` so the knowledge base's pgvector
     tables can build (the pgvector image ships the extension, but someone
     pointing compose at their own Postgres may need to install it);
  3. seeds the first admin — username from CFOP_ADMIN_USERNAME (default
     "admin"), password from CFOP_ADMIN_PASSWORD or generated and printed
     ONCE to the service log;
  4. mints the `cfoperator-event-runtime` API token (read+investigate, the
     scope pairing from the CFOP-10 service-credential table) and writes it
     to a file on the shared volume, which the event-runtime service sources
     at startup. The secret is unrecoverable by design, so if the file is
     gone but a live token row exists (e.g. the shared volume was recreated
     but the database kept its), the old token is revoked and a new one
     minted — idempotence is keyed on the file, not the row.

Safe to re-run: an existing admin is left alone, a token whose file is
present is left alone. Exit code 0 means both seeds are in place.
"""

import os
import secrets
import sys
import time

sys.path.insert(0, ".")

from auth.models import ROLE_ADMIN  # noqa: E402
from auth.store import AuthStore, default_db_url  # noqa: E402

TOKEN_LABEL = "cfoperator-event-runtime"
TOKEN_SCOPES = ["read", "investigate"]
DEFAULT_TOKEN_FILE = "/shared/event-runtime.env"
DEFAULT_AGENT_ENV_FILE = "/shared/agent.env"


def log(msg: str) -> None:
    print(f"[compose-bootstrap] {msg}", flush=True)


def wait_for_db(timeout_seconds: int = 90) -> AuthStore:
    """Retry until Postgres accepts connections and the auth schema exists."""
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            store = AuthStore()
            store.ensure_schema()
            log("database reachable, auth schema ensured")
            return store
        except Exception as exc:  # noqa: BLE001 - connection refused, DNS, etc.
            last_error = exc
            time.sleep(2)
    raise SystemExit(f"database not reachable after {timeout_seconds}s: {last_error}")


def ensure_pgvector() -> None:
    """Best-effort CREATE EXTENSION vector; the KB needs it, the trial image has it."""
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(default_db_url())
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        log("pgvector extension present")
    except Exception as exc:  # noqa: BLE001 - non-superuser against external PG
        log(
            "could not ensure pgvector extension (continuing; knowledge-base "
            f"embeddings will fail until it is installed): {exc}"
        )


def seed_admin(store: AuthStore) -> None:
    # Keyed on "is there a usable admin", not "are there any users at all":
    # a database holding only members (or only a deactivated admin) is exactly
    # the locked-out state this seeding exists to prevent, and counting users
    # would skip it.
    if any(u["role"] == ROLE_ADMIN and u["is_active"] for u in store.list_users()):
        log("an active admin exists — seeding skipped")
        return
    username = os.getenv("CFOP_ADMIN_USERNAME", "").strip() or "admin"
    if store.get_user_by_username(username) is not None:
        # The name is taken by someone who is not an active admin. Refuse
        # rather than silently promoting or overwriting an existing account.
        log(
            f"cannot seed {username!r}: a user of that name already exists but "
            "is not an active admin. Recover with scripts/create_admin.py "
            f"{username} --reset, or set CFOP_ADMIN_USERNAME to a free name."
        )
        return
    password = os.getenv("CFOP_ADMIN_PASSWORD", "").strip()
    generated = not password
    if generated:
        password = secrets.token_urlsafe(12)
    store.create_user(username, password, role=ROLE_ADMIN, actor="compose_bootstrap")
    if generated:
        # The one place this can ever be shown; it is not stored in plaintext.
        log(
            f"seeded admin {username!r} with GENERATED password: {password}\n"
            "[compose-bootstrap] Save it now (or set CFOP_ADMIN_PASSWORD in .env "
            "and recreate the volumes to choose your own)."
        )
    else:
        log(f"seeded admin {username!r} from CFOP_ADMIN_PASSWORD")


def ensure_event_runtime_token(store: AuthStore) -> None:
    token_file = os.getenv("CFOP_BOOTSTRAP_TOKEN_FILE", DEFAULT_TOKEN_FILE)
    # `status` rather than `revoked_at`: an expired token is also unusable, and
    # filtering on revocation alone would skip minting and hand event_runtime a
    # credential the agent rejects.
    active = [
        row
        for row in store.list_tokens()
        if row["label"] == TOKEN_LABEL and row["status"] == "active"
    ]
    if os.path.exists(token_file) and active:
        log(f"event-runtime token present ({active[0]['token_prefix']}…) — skipped")
        return

    # File missing (or no live row): the secret behind any surviving row is
    # unrecoverable, so revoke and mint fresh rather than leaving a zombie.
    for row in active:
        store.revoke_token(row["id"], actor="compose_bootstrap")
        log(f"revoked orphaned token {row['token_prefix']}… (its file was gone)")

    row, secret = store.create_token(TOKEN_LABEL, TOKEN_SCOPES, creator_role=ROLE_ADMIN)
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(f"CFOP_API_TOKEN={secret}\n")
    log(
        f"minted event-runtime token {row['token_prefix']}… "
        f"(scopes={TOKEN_SCOPES}) → {token_file}"
    )


def ensure_session_secret() -> None:
    """Persist a Flask session key, without which login is a 500, not a downgrade.

    ``web_auth.py`` declines to set ``app.secret_key`` when CFOP_SESSION_SECRET
    is empty, so ``session.clear()`` on the login path raises and every attempt
    fails with a 500. Generating it here keeps the trial's promise that only
    three values need setting, and persisting it means sessions survive a
    restart instead of silently logging everyone out.
    """
    env_file = os.getenv("CFOP_BOOTSTRAP_AGENT_ENV_FILE", DEFAULT_AGENT_ENV_FILE)
    existing = ""
    if os.path.exists(env_file):
        with open(env_file) as fh:
            existing = fh.read()
        if "CFOP_SESSION_SECRET=" in existing:
            log("session secret present — skipped")
            return
        if existing and not existing.endswith("\n"):
            existing += "\n"
    # Append rather than truncate: this file is sourced wholesale by the agent,
    # so anything else written into it (by a later bootstrap step, or by hand)
    # would be silently destroyed by a rewrite.
    fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(f"{existing}CFOP_SESSION_SECRET={secrets.token_urlsafe(48)}\n")
    log(f"generated session secret → {env_file}")


def main() -> int:
    store = wait_for_db()
    ensure_pgvector()
    seed_admin(store)
    ensure_session_secret()
    ensure_event_runtime_token(store)
    log("bootstrap complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
