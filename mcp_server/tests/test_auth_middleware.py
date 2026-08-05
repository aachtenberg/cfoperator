"""Per-token bearer auth at the ASGI edge.

Runs the middleware directly against a real (SQLite) AuthStore rather than the
assembled server: the properties that matter — a revoked token stops working, a
database outage does not fall open — are exactly the ones a mocked store would
wave through. Importing the FastMCP app here would also drag the MCP SDK into a
test about twelve lines of ASGI.
"""

import asyncio
import json

import pytest
from sqlalchemy import create_engine

from auth.models import ROLE_ADMIN
from auth.store import AuthStore
from mcp_server.auth import (
    SCOPE_KEY,
    BearerAuthMiddleware,
    ScopeError,
    acting_token,
    granted_scopes,
    require_scope,
)
from mcp_server.config import Settings

LEGACY_TOKEN = "shared-mvp-token"


@pytest.fixture
def store():
    s = AuthStore(engine=create_engine("sqlite://"))
    s.ensure_schema()
    return s


@pytest.fixture
def admin(store):
    return store.create_user("root", "hunter2", role=ROLE_ADMIN)


class ExplodingStore:
    """Stands in for PostgreSQL being unreachable mid-request."""

    def verify_token(self, presented):
        raise RuntimeError("connection refused")


def probe(auth_header=None, token=LEGACY_TOKEN, store=None, path="/mcp"):
    """Drive one request through the middleware; report what got through."""
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent = []

    async def send(message):
        sent.append(message)

    headers = [(b"authorization", auth_header.encode())] if auth_header else []
    scope = {"type": "http", "headers": headers, "path": path}
    mw = BearerAuthMiddleware(inner, token=token, store=store)
    asyncio.run(mw(scope, lambda: None, send))
    return seen, sent


def body(sent):
    return json.loads(sent[1]["body"])


# ---- database-backed tokens -------------------------------------------


def test_valid_db_token_authenticates_and_carries_its_own_scopes(store, admin):
    _row, secret = store.create_token("dashboard", ["read"], created_by=admin["id"])
    seen, sent = probe(f"Bearer {secret}", store=store)

    assert sent[0]["status"] == 200
    identity = seen[0][SCOPE_KEY]
    assert identity.label == "dashboard"
    assert identity.scopes == frozenset({"read"})
    # The process env grant is wider than the token; the token must win.
    assert granted_scopes(seen[0], Settings(scopes=frozenset(
        {"read", "investigate", "remediate"}))) == frozenset({"read"})


def test_two_tokens_on_one_process_get_different_scopes(store, admin):
    _r1, reader = store.create_token("reader", ["read"], created_by=admin["id"])
    _r2, fixer = store.create_token("fixer", ["remediate"], created_by=admin["id"])
    settings = Settings(scopes=frozenset({"read"}))

    reader_scope, _ = probe(f"Bearer {reader}", store=store)
    fixer_scope, _ = probe(f"Bearer {fixer}", store=store)

    assert granted_scopes(reader_scope[0], settings) == frozenset({"read"})
    assert granted_scopes(fixer_scope[0], settings) == frozenset(
        {"read", "investigate", "remediate"})


def test_revoked_token_is_rejected(store, admin):
    row, secret = store.create_token("doomed", ["read"], created_by=admin["id"])
    assert probe(f"Bearer {secret}", store=store)[1][0]["status"] == 200

    store.revoke_token(row["id"])
    seen, sent = probe(f"Bearer {secret}", store=store)
    assert seen == []
    assert sent[0]["status"] == 401
    assert body(sent)["code"] == "unauthorized"


def test_expired_token_is_rejected(store, admin):
    from datetime import timedelta

    from auth.models import ApiToken, utcnow

    _row, secret = store.create_token(
        "short", ["read"], created_by=admin["id"], expires_in_days=1)

    session = store._session()
    session.query(ApiToken).first().expires_at = utcnow() - timedelta(seconds=1)
    session.commit()
    session.close()

    seen, sent = probe(f"Bearer {secret}", store=store)
    assert seen == []
    assert sent[0]["status"] == 401


def test_unknown_db_token_does_not_fall_through_to_a_200(store, admin):
    store.create_token("real", ["read"], created_by=admin["id"])
    seen, sent = probe("Bearer cfop_nosuchtokenatall0000000000000000", store=store)
    assert seen == []
    assert sent[0]["status"] == 401


# ---- legacy shared token ----------------------------------------------


def test_legacy_shared_token_still_works_without_a_store():
    seen, sent = probe(f"Bearer {LEGACY_TOKEN}")
    assert sent[0]["status"] == 200
    assert seen[0][SCOPE_KEY].legacy is True


def test_legacy_shared_token_still_works_alongside_a_store(store, admin, caplog):
    store.create_token("real", ["read"], created_by=admin["id"])
    with caplog.at_level("WARNING", logger="mcp_server.auth"):
        seen, sent = probe(f"Bearer {LEGACY_TOKEN}", store=store)

    assert sent[0]["status"] == 200
    assert seen[0][SCOPE_KEY].legacy is True
    assert any("deprecated shared CFOP_MCP_TOKEN" in r.message for r in caplog.records)
    # Recorded so the migration can be declared finished on evidence.
    assert store.recent_audit(event="token.legacy_used")[0]["detail"]["path"] == "/mcp"


def test_legacy_token_keeps_the_process_scope_grant():
    """The shared token has no scopes of its own — inventing a full set for it
    would widen the credential the migration exists to narrow."""
    seen, _sent = probe(f"Bearer {LEGACY_TOKEN}")
    settings = Settings(scopes=frozenset({"read"}))
    assert granted_scopes(seen[0], settings) == frozenset({"read"})


def test_db_token_is_not_shadowed_by_the_legacy_token(store, admin):
    _row, secret = store.create_token("mcp", ["investigate"], created_by=admin["id"])
    seen, _sent = probe(f"Bearer {secret}", store=store)
    assert seen[0][SCOPE_KEY].legacy is False
    assert seen[0][SCOPE_KEY].label == "mcp"


@pytest.mark.parametrize("bad", [None, "Bearer nope", "Basic " + LEGACY_TOKEN,
                                 LEGACY_TOKEN])
def test_missing_and_malformed_credentials_rejected(bad, store):
    for with_store in (None, store):
        seen, sent = probe(bad, store=with_store)
        assert seen == []
        assert sent[0]["status"] == 401
        assert body(sent) == {
            "code": "unauthorized",
            "message": "missing or invalid bearer token",
            "retryable": False,
        }


def test_no_credentials_configured_at_all_rejects_everything():
    seen, sent = probe("Bearer anything", token=None, store=None)
    assert seen == []
    assert sent[0]["status"] == 401


# ---- database outage ---------------------------------------------------


def test_db_error_yields_503_not_200():
    seen, sent = probe("Bearer cfop_whatever000000000000000000000000",
                       store=ExplodingStore())
    assert seen == []
    assert sent[0]["status"] == 503
    assert body(sent) == {
        "code": "auth_unavailable",
        "message": "could not verify bearer token",
        "retryable": True,
    }


def test_db_error_does_not_fall_through_to_the_legacy_token():
    """Otherwise an outage would silently promote every caller to the shared
    token's process-wide grant."""
    seen, sent = probe(f"Bearer {LEGACY_TOKEN}", store=ExplodingStore())
    assert seen == []
    assert sent[0]["status"] == 503


# ---- non-http scopes ---------------------------------------------------


def test_websocket_and_lifespan_scopes_pass_through(store):
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope)

    mw = BearerAuthMiddleware(inner, token=LEGACY_TOKEN, store=store)
    asyncio.run(mw({"type": "lifespan"}, lambda: None, lambda m: None))
    assert len(seen) == 1


# ---- scope resolution --------------------------------------------------


def test_require_scope_denies_when_the_token_lacks_the_tier(store, admin):
    _row, secret = store.create_token("reader", ["read"], created_by=admin["id"])
    seen, _sent = probe(f"Bearer {secret}", store=store)
    granted = granted_scopes(seen[0], Settings(scopes=frozenset({"remediate"})))

    require_scope(granted, "read")
    with pytest.raises(ScopeError, match="requires scope 'remediate'"):
        require_scope(granted, "remediate")


def test_stdio_has_no_asgi_scope_and_keeps_the_env_grant():
    settings = Settings(scopes=frozenset({"read", "investigate"}))
    assert granted_scopes(None, settings) == settings.scopes
    assert acting_token(None) is None


def test_acting_token_names_the_credential_for_the_audit_line(store, admin):
    _row, secret = store.create_token("event-runtime", ["read"], created_by=admin["id"])
    seen, _sent = probe(f"Bearer {secret}", store=store)
    assert acting_token(seen[0]).label == "event-runtime"
    assert acting_token(seen[0]).token_id is not None
