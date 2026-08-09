"""Tests for the auth store: users, tokens, and the guards around them.

Run against a real (SQLite) database rather than a mock, because the properties
worth testing here — the last-admin guard, token expiry and revocation, the
scope ceiling — are the ones a mock would happily let through.
"""

import os

import pytest
from sqlalchemy import create_engine

from auth.bootstrap import bootstrap_admin
from auth.models import ROLE_ADMIN, ROLE_MEMBER, expand_scopes
from auth.store import AuthError, AuthStore
from auth.tokens import TOKEN_PREFIX, hash_token, mint_token


@pytest.fixture
def store():
    s = AuthStore(engine=create_engine("sqlite://"))
    s.ensure_schema()
    return s


@pytest.fixture
def admin(store):
    return store.create_user("root", "hunter2", role=ROLE_ADMIN)


# ---- users ------------------------------------------------------------


def test_create_and_fetch_user(store):
    created = store.create_user("alice", "s3cret", role=ROLE_MEMBER)
    assert created["username"] == "alice"
    assert created["role"] == ROLE_MEMBER
    assert created["is_active"] is True
    # The hash must never reach a response body.
    assert "password_hash" not in created

    assert store.get_user(created["id"])["username"] == "alice"
    assert store.get_user_by_username("alice")["id"] == created["id"]


def test_usernames_are_unique_case_insensitively(store):
    store.create_user("alice", "pw")
    # "Alice" and "alice" logging in as different people would be a phishing
    # gift inside a small team.
    with pytest.raises(AuthError, match="already exists"):
        store.create_user("Alice", "pw")


def test_unknown_role_rejected(store):
    with pytest.raises(AuthError, match="role must be one of"):
        store.create_user("bob", "pw", role="superuser")


def test_login_succeeds_and_stamps_last_login(store):
    store.create_user("alice", "correct horse")
    assert store.get_user_by_username("alice")["last_login_at"] is None

    user = store.verify_login("alice", "correct horse")
    assert user and user["username"] == "alice"
    assert store.get_user_by_username("alice")["last_login_at"] is not None


@pytest.mark.parametrize("username,password", [
    ("alice", "wrong"),
    ("nobody", "correct horse"),
    ("alice", ""),
    ("alice", "correct horse "),   # trailing space must not pass
])
def test_bad_logins_rejected(store, username, password):
    store.create_user("alice", "correct horse")
    assert store.verify_login(username, password) is None


def test_deactivated_user_cannot_log_in(store):
    user = store.create_user("alice", "pw")
    store.create_user("root", "pw", role=ROLE_ADMIN)
    store.update_user(user["id"], is_active=False)
    assert store.verify_login("alice", "pw") is None


def test_cannot_demote_or_deactivate_the_last_admin(store, admin):
    """Losing every admin means losing user management itself — recoverable
    only by re-seeding from the environment."""
    with pytest.raises(AuthError, match="last active admin"):
        store.update_user(admin["id"], role=ROLE_MEMBER)
    with pytest.raises(AuthError, match="last active admin"):
        store.update_user(admin["id"], is_active=False)


def test_can_demote_an_admin_when_another_remains(store, admin):
    second = store.create_user("second", "pw", role=ROLE_ADMIN)
    demoted = store.update_user(second["id"], role=ROLE_MEMBER)
    assert demoted["role"] == ROLE_MEMBER


def test_deactivated_admin_does_not_count_toward_the_guard(store, admin):
    spare = store.create_user("spare", "pw", role=ROLE_ADMIN)
    store.update_user(spare["id"], is_active=False)
    # `spare` is an admin on paper but cannot log in, so it must not be what
    # allows the real admin to be removed.
    with pytest.raises(AuthError, match="last active admin"):
        store.update_user(admin["id"], is_active=False)


def test_password_change_invalidates_the_old_password(store):
    user = store.create_user("alice", "old")
    store.set_password(user["id"], "new")
    assert store.verify_login("alice", "old") is None
    assert store.verify_login("alice", "new") is not None


# ---- tokens -----------------------------------------------------------


def test_mint_token_shape():
    secret = mint_token()
    assert secret.startswith(TOKEN_PREFIX)
    assert len(secret) > len(TOKEN_PREFIX) + 30
    assert mint_token() != mint_token()


def test_create_token_returns_secret_once_and_stores_only_the_digest(store, admin):
    row, secret = store.create_token("event_runtime", ["investigate"], created_by=admin["id"])
    assert secret.startswith(TOKEN_PREFIX)
    # The row the API returns must never carry the secret or its digest.
    assert secret not in str(row)
    assert "token_hash" not in row
    assert row["token_prefix"] == secret[:len(TOKEN_PREFIX) + 8]

    listed = store.list_tokens()
    assert len(listed) == 1 and listed[0]["status"] == "active"


def test_verify_token_resolves_identity_with_implied_scopes(store, admin):
    _row, secret = store.create_token("mcp", ["remediate"], created_by=admin["id"])
    identity = store.verify_token(secret)
    assert identity is not None
    assert identity.label == "mcp"
    assert identity.user_id == admin["id"]
    # remediate implies investigate implies read
    assert identity.has_scope("remediate")
    assert identity.has_scope("investigate")
    assert identity.has_scope("read")


def test_read_only_token_does_not_get_remediate(store, admin):
    _row, secret = store.create_token("dashboard", ["read"], created_by=admin["id"])
    identity = store.verify_token(secret)
    assert identity.has_scope("read")
    assert not identity.has_scope("investigate")
    assert not identity.has_scope("remediate")


@pytest.mark.parametrize("bad", [
    "",
    "not-a-token",
    "cfop_wrong",
    "Bearer cfop_abc",
])
def test_invalid_tokens_rejected(store, admin, bad):
    store.create_token("real", ["read"], created_by=admin["id"])
    assert store.verify_token(bad) is None


def test_truncated_and_extended_tokens_rejected(store, admin):
    """Catches any comparison that matches on a prefix rather than the whole."""
    _row, secret = store.create_token("real", ["read"], created_by=admin["id"])
    assert store.verify_token(secret[:-1]) is None
    assert store.verify_token(secret + "x") is None


def test_revoked_token_stops_working(store, admin):
    row, secret = store.create_token("doomed", ["read"], created_by=admin["id"])
    assert store.verify_token(secret) is not None

    store.revoke_token(row["id"])
    assert store.verify_token(secret) is None
    assert store.list_tokens()[0]["status"] == "revoked"


def test_expired_token_stops_working(store, admin):
    from datetime import timedelta

    from auth.models import ApiToken, utcnow

    _row, secret = store.create_token("short", ["read"], created_by=admin["id"], expires_in_days=1)
    assert store.verify_token(secret) is not None

    # Wind the expiry back rather than sleeping.
    session = store._session()
    token = session.query(ApiToken).first()
    token.expires_at = utcnow() - timedelta(seconds=1)
    session.commit()
    session.close()

    assert store.verify_token(secret) is None


def test_token_of_deactivated_owner_is_refused(store, admin):
    """A revoked human must not keep authenticating through a token they
    minted while they still had an account."""
    member = store.create_user("alice", "pw", role=ROLE_MEMBER)
    _row, secret = store.create_token("alice-laptop", ["read"], created_by=member["id"])
    assert store.verify_token(secret) is not None

    store.update_user(member["id"], is_active=False)
    assert store.verify_token(secret) is None


def test_member_cannot_mint_a_remediate_token(store):
    """Otherwise token creation is a privilege-escalation path around the role."""
    member = store.create_user("alice", "pw", role=ROLE_MEMBER)
    with pytest.raises(AuthError, match="cannot grant scopes"):
        store.create_token(
            "escalate", ["remediate"], created_by=member["id"], creator_role=ROLE_MEMBER,
        )


def test_admin_can_mint_a_remediate_token(store, admin):
    row, _secret = store.create_token(
        "executor", ["remediate"], created_by=admin["id"], creator_role=ROLE_ADMIN,
    )
    assert row["scopes"] == ["remediate"]


def test_unknown_scope_rejected(store, admin):
    with pytest.raises(AuthError, match="unknown scopes"):
        store.create_token("weird", ["superuser"], created_by=admin["id"])


def test_token_requires_a_label_and_a_scope(store, admin):
    with pytest.raises(AuthError, match="label is required"):
        store.create_token("", ["read"], created_by=admin["id"])
    with pytest.raises(AuthError, match="at least one scope"):
        store.create_token("empty", [], created_by=admin["id"])


def test_list_tokens_can_be_scoped_to_one_user(store, admin):
    other = store.create_user("alice", "pw")
    store.create_token("admins", ["read"], created_by=admin["id"])
    store.create_token("alices", ["read"], created_by=other["id"])

    assert len(store.list_tokens()) == 2
    mine = store.list_tokens(created_by=other["id"])
    assert [t["label"] for t in mine] == ["alices"]


# ---- scope expansion --------------------------------------------------


@pytest.mark.parametrize("granted,expected", [
    (["read"], {"read"}),
    (["investigate"], {"investigate", "read"}),
    (["remediate"], {"remediate", "investigate", "read"}),
    ([], set()),
    (None, set()),
])
def test_scope_expansion(granted, expected):
    assert expand_scopes(granted) == expected


# ---- audit ------------------------------------------------------------


def test_audit_events_are_recorded_and_queryable(store):
    store.record("login.success", actor="alice", source_ip="10.0.0.5")
    store.record("token.created", actor="alice", target="cfop_abc")

    rows = store.recent_audit()
    assert {r["event"] for r in rows} == {"login.success", "token.created"}

    only_logins = store.recent_audit(event="login.success")
    assert len(only_logins) == 1
    assert only_logins[0]["actor"] == "alice"
    assert only_logins[0]["source_ip"] == "10.0.0.5"


def test_legacy_token_use_is_auditable(store):
    """The migration is finished when this query comes back empty."""
    store.record_legacy_token_use("/api/remediations/1/approve", source_ip="10.0.0.9")
    rows = store.recent_audit(event="token.legacy_used")
    assert len(rows) == 1
    assert rows[0]["detail"]["path"] == "/api/remediations/1/approve"


# ---- bootstrap --------------------------------------------------------


def test_bootstrap_seeds_first_admin_from_legacy_env(store, monkeypatch):
    from werkzeug.security import generate_password_hash

    monkeypatch.setenv("CFOP_UI_USERNAME", "operator")
    monkeypatch.setenv("CFOP_UI_PASSWORD_HASH", generate_password_hash("legacy-pw"))

    assert bootstrap_admin(store) is True
    # The operator's existing password must still work — the hash is copied
    # across, never re-hashed.
    user = store.verify_login("operator", "legacy-pw")
    assert user and user["role"] == ROLE_ADMIN


def test_bootstrap_is_a_no_op_once_a_user_exists(store, monkeypatch):
    from werkzeug.security import generate_password_hash

    store.create_user("alice", "pw", role=ROLE_ADMIN)
    monkeypatch.setenv("CFOP_UI_USERNAME", "operator")
    monkeypatch.setenv("CFOP_UI_PASSWORD_HASH", generate_password_hash("legacy-pw"))

    # Re-adding the env vars must not resurrect a bootstrap admin alongside
    # real accounts.
    assert bootstrap_admin(store) is False
    assert store.get_user_by_username("operator") is None


def test_bootstrap_without_env_credentials_does_nothing(store, monkeypatch):
    monkeypatch.delenv("CFOP_UI_USERNAME", raising=False)
    monkeypatch.delenv("CFOP_UI_PASSWORD_HASH", raising=False)
    assert bootstrap_admin(store) is False
    assert store.count_users() == 0


# ---- hashing ----------------------------------------------------------


def test_token_hash_is_stable_and_distinct():
    a, b = mint_token(), mint_token()
    assert hash_token(a) == hash_token(a)
    assert hash_token(a) != hash_token(b)
    assert len(hash_token(a)) == 64
