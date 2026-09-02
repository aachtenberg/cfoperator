"""Guards for the trial docker-compose stack (CFOP-25).

The class of regression these exist to catch: the getting-started path quietly
regaining homelab assumptions. The default compose once was host-network with
``~/.ssh`` and ``docker.sock`` mounted and no event_runtime — a stack that only
boots on the machine it was written on. If someone re-adds any of that to the
default file (instead of the extras overlay), or drops a service the trial
needs, these fail.

Also covers first-boot seeding (scripts/compose_bootstrap.py) against a real
AuthStore on sqlite: the trial dead-ends if the admin or the event-runtime
token don't materialise, and re-runs must not multiply either.
"""

from repo_paths import REPO_ROOT
import os
from pathlib import Path

import pytest
import yaml

REPO = REPO_ROOT

TRIAL_SERVICES = {"postgres", "bootstrap", "agent", "event-runtime"}


def _load(path: str) -> dict:
    with open(REPO / path) as fh:
        return yaml.safe_load(fh)


# ── default compose: the trial contract ──────────────────────────────────────

def test_default_compose_has_trial_services():
    services = _load("docker-compose.yml")["services"]
    missing = TRIAL_SERVICES - set(services)
    assert not missing, f"trial compose lost services: {missing}"


def test_default_compose_has_no_homelab_assumptions():
    compose = _load("docker-compose.yml")
    for name, svc in compose["services"].items():
        assert svc.get("network_mode") != "host", (
            f"service {name!r} uses host networking — that belongs nowhere; "
            "the trial file must run on a clean machine with bridged networking"
        )
        for vol in svc.get("volumes", []):
            vol_str = vol if isinstance(vol, str) else str(vol)
            assert ".ssh" not in vol_str, (
                f"service {name!r} mounts SSH keys — hybrid-fleet access lives in "
                "docker-compose.extras.yml, not the default file"
            )
            assert "docker.sock" not in vol_str, (
                f"service {name!r} mounts docker.sock — that is extras-overlay "
                "territory, not the trial default"
            )


def test_default_compose_orders_first_boot():
    services = _load("docker-compose.yml")["services"]
    boot_deps = services["bootstrap"].get("depends_on", {})
    assert boot_deps.get("postgres", {}).get("condition") == "service_healthy"
    agent_deps = services["agent"].get("depends_on", {})
    assert (
        agent_deps.get("bootstrap", {}).get("condition")
        == "service_completed_successfully"
    ), "agent must wait for first-boot seeding or the login page is a dead end"


def test_default_compose_publishes_console():
    agent = _load("docker-compose.yml")["services"]["agent"]
    assert any("8083" in str(p) for p in agent.get("ports", [])), (
        "console port 8083 is not published"
    )


# ── extras overlay: the capability must survive, opt-in ──────────────────────

def test_extras_overlay_carries_fleet_mounts():
    overlay = _load("docker-compose.extras.yml")["services"]
    agent_vols = " ".join(str(v) for v in overlay["agent"].get("volumes", []))
    assert ".ssh" in agent_vols and "docker.sock" in agent_vols, (
        "the extras overlay lost the hybrid-fleet mounts — moving them out of "
        "the default file must not delete the capability"
    )


# ── starter config: minimal and inventory-free ───────────────────────────────

def test_starter_config_is_valid_and_inventory_free():
    cfg = _load("deploy/compose/config.yaml")
    assert "infrastructure" not in cfg, (
        "the starter config grew a host inventory — discovery, not inventory, "
        "is the trial contract"
    )
    assert "observability" not in cfg, (
        "the starter config went back to the canonical long form — the trial "
        "config is the short alias form, which is the surface a getting-started "
        "file (and the Helm chart) should template"
    )
    assert cfg["profile"] == "investigate", "the trial must default to read-only"
    assert cfg["llm"]["backend"] == "ollama"
    assert "prometheus" in cfg and "alertmanager" in cfg


def test_starter_config_relies_on_merged_defaults():
    """The predecessor of this test asserted the *opposite*: that the starter
    config spelled out every key the agent hard-indexes, because a config file
    that existed suppressed the built-in defaults wholesale and a missing
    ``ooda`` crash-looped the main loop with KeyError.

    Partial configs now merge over complete defaults, so spelling those
    sections out is no longer required — and re-adding them would quietly
    re-couple the trial config to internals it should not know about. This
    guards that they stay gone.
    """
    cfg = _load("deploy/compose/config.yaml")
    for section in ("ooda", "llm_fallback", "git", "skills"):
        assert section not in cfg, (
            f"{section!r} is back in the starter config — it has a working "
            "default, so naming it here is noise the trial user must read"
        )


# ── first-boot seeding, against a real store ─────────────────────────────────

@pytest.fixture()
def store(tmp_path):
    from auth.store import AuthStore

    s = AuthStore(db_url=f"sqlite:///{tmp_path}/auth.db")
    s.ensure_schema()
    return s


def test_bootstrap_seeds_admin_once(store, tmp_path, monkeypatch):
    import scripts.compose_bootstrap as boot

    monkeypatch.setenv("CFOP_ADMIN_USERNAME", "trial-admin")
    monkeypatch.setenv("CFOP_ADMIN_PASSWORD", "hunter2hunter2")
    boot.seed_admin(store)
    boot.seed_admin(store)  # idempotent — a second boot must not duplicate
    users = store.list_users()
    assert [u["username"] for u in users] == ["trial-admin"]
    assert users[0]["role"] == "admin"
    assert store.verify_login("trial-admin", "hunter2hunter2") is not None


def test_bootstrap_seeds_when_only_non_admins_exist(store, monkeypatch):
    """Counting users would skip seeding here, leaving a database with members
    but no way in — the exact lockout the seeding exists to prevent.
    """
    import scripts.compose_bootstrap as boot

    store.create_user("someone", "memberpassword", role="member", actor="test")
    monkeypatch.setenv("CFOP_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("CFOP_ADMIN_PASSWORD", "adminpassword")
    boot.seed_admin(store)
    roles = {u["username"]: u["role"] for u in store.list_users()}
    assert roles == {"someone": "member", "admin": "admin"}


def test_bootstrap_refuses_to_hijack_an_existing_username(store, monkeypatch, capsys):
    """If the desired name is taken by a non-admin, promoting or overwriting it
    would be a privilege escalation performed by a boot script.
    """
    import scripts.compose_bootstrap as boot

    store.create_user("admin", "memberpassword", role="member", actor="test")
    monkeypatch.setenv("CFOP_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("CFOP_ADMIN_PASSWORD", "adminpassword")
    boot.seed_admin(store)
    assert store.get_user_by_username("admin")["role"] == "member"
    assert "cannot seed" in capsys.readouterr().out


def test_bootstrap_generates_password_when_unset(store, monkeypatch, capsys):
    import scripts.compose_bootstrap as boot

    monkeypatch.delenv("CFOP_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("CFOP_ADMIN_USERNAME", raising=False)
    boot.seed_admin(store)
    out = capsys.readouterr().out
    assert "GENERATED password" in out, "generated credentials must be printed once"


def test_bootstrap_generates_session_secret(tmp_path, monkeypatch):
    """Without this, login is a 500 — web_auth.py leaves app.secret_key unset
    when CFOP_SESSION_SECRET is empty, so session.clear() raises on /login.
    Found by booting the stack; no unit test reached it.
    """
    import scripts.compose_bootstrap as boot

    env_file = tmp_path / "agent.env"
    monkeypatch.setenv("CFOP_BOOTSTRAP_AGENT_ENV_FILE", str(env_file))

    boot.ensure_session_secret()
    first = env_file.read_text()
    assert first.startswith("CFOP_SESSION_SECRET=")
    assert len(first.split("=", 1)[1].strip()) >= 32
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"

    boot.ensure_session_secret()  # stable across restarts, or sessions break
    assert env_file.read_text() == first


def test_session_secret_preserves_other_vars_in_the_file(tmp_path, monkeypatch):
    """The agent sources this file wholesale, so a rewrite that dropped other
    lines would silently unset whatever a later step (or a human) put there.
    """
    import scripts.compose_bootstrap as boot

    env_file = tmp_path / "agent.env"
    env_file.write_text("SOMETHING_ELSE=keepme")  # no trailing newline, on purpose
    monkeypatch.setenv("CFOP_BOOTSTRAP_AGENT_ENV_FILE", str(env_file))

    boot.ensure_session_secret()
    body = env_file.read_text()
    assert "SOMETHING_ELSE=keepme" in body
    assert "CFOP_SESSION_SECRET=" in body
    assert "keepmeCFOP_SESSION_SECRET" not in body, "missing newline joined two vars"


def test_expired_token_is_replaced_not_treated_as_active(store, tmp_path, monkeypatch):
    """`revoked_at is None` is not the same as usable: an expired token would
    have been left in place and event_runtime would start with a credential the
    agent rejects.
    """
    import scripts.compose_bootstrap as boot

    token_file = tmp_path / "event-runtime.env"
    monkeypatch.setenv("CFOP_BOOTSTRAP_TOKEN_FILE", str(token_file))
    boot.ensure_event_runtime_token(store)
    old = token_file.read_text().split("=", 1)[1].strip()

    # Age the token out without revoking it.
    from datetime import timedelta

    from auth.models import ApiToken
    from auth.store import utcnow

    session = store._session()
    try:
        row = session.query(ApiToken).filter(ApiToken.label == boot.TOKEN_LABEL).one()
        row.expires_at = utcnow() - timedelta(days=1)
        session.commit()
    finally:
        session.close()
    assert store.verify_token(old) is None, "precondition: the token is now unusable"

    boot.ensure_event_runtime_token(store)
    new = token_file.read_text().split("=", 1)[1].strip()
    assert new != old
    assert store.verify_token(new) is not None


def test_agent_service_sources_the_session_secret():
    agent = _load("docker-compose.yml")["services"]["agent"]
    command = " ".join(agent.get("command", []))
    assert "/shared/agent.env" in command, (
        "the agent no longer sources the generated session secret — login 500s"
    )
    assert any("shared-run" in str(v) for v in agent.get("volumes", [])), (
        "the agent cannot read /shared/agent.env without the shared volume"
    )


def test_bootstrap_token_minted_and_idempotent(store, tmp_path, monkeypatch):
    import scripts.compose_bootstrap as boot

    token_file = tmp_path / "event-runtime.env"
    monkeypatch.setenv("CFOP_BOOTSTRAP_TOKEN_FILE", str(token_file))

    boot.ensure_event_runtime_token(store)
    assert token_file.exists()
    assert oct(token_file.stat().st_mode & 0o777) == "0o600"
    secret = token_file.read_text().strip().split("=", 1)[1]
    identity = store.verify_token(secret)
    assert identity is not None
    assert identity.has_scope("investigate") and not identity.has_scope("remediate")

    # Second run with the file present: no new token.
    boot.ensure_event_runtime_token(store)
    live = [t for t in store.list_tokens() if not t["revoked_at"]]
    assert len(live) == 1

    # File lost (volume recreated): old row revoked, fresh secret written.
    token_file.unlink()
    boot.ensure_event_runtime_token(store)
    new_secret = token_file.read_text().strip().split("=", 1)[1]
    assert new_secret != secret
    assert store.verify_token(secret) is None, "orphaned token must be revoked"
    assert store.verify_token(new_secret) is not None


def test_bootstrap_db_only_skips_file_seeding(store, monkeypatch):
    """CFOP-30: the Helm chart provides session secret + API token via
    Secrets; its bootstrap Job runs DB-only. Without the flag honored, every
    `helm upgrade` would revoke + remint a DB token nobody reads (idempotence
    is keyed on a file that lives in the Job's emptyDir)."""
    import scripts.compose_bootstrap as boot

    calls = []
    monkeypatch.setattr(boot, "wait_for_db", lambda *a: calls.append("db") or store)
    monkeypatch.setattr(boot, "ensure_pgvector", lambda: calls.append("pgvector"))
    monkeypatch.setattr(boot, "seed_admin", lambda s: calls.append("admin"))
    monkeypatch.setattr(boot, "ensure_session_secret", lambda: calls.append("session"))
    monkeypatch.setattr(boot, "ensure_event_runtime_token", lambda s: calls.append("token"))

    monkeypatch.setenv("CFOP_BOOTSTRAP_DB_ONLY", "true")
    assert boot.main() == 0
    assert calls == ["db", "pgvector", "admin"]

    calls.clear()
    monkeypatch.delenv("CFOP_BOOTSTRAP_DB_ONLY")
    assert boot.main() == 0
    assert calls == ["db", "pgvector", "admin", "session", "token"]
