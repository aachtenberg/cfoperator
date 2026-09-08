"""SSH identity + inventory config for a cockpit session (CFOP-146)."""

from repo_paths import REPO_ROOT
from pathlib import Path

from cockpit.ssh import (
    SSH_BUNDLE_ENV,
    decode_ssh_bundle,
    encode_ssh_bundle,
    fleet_ssh_config,
    identity_paths_from_hosts,
    load_identity_files,
    session_ssh_files,
)


HOSTS = {
    "raspberrypi5": {"address": "10.0.0.15", "ssh": {"user": "sre",
                                                     "key_path": "/keys/id_rsa"}},
    "ubuntu-llm-01": {"address": "10.0.0.20", "ssh": {"user": "aachten"}},
}


def test_fleet_ssh_config_aliases_inventory_names():
    cfg = fleet_ssh_config(HOSTS, default_user="sre")
    assert "Host raspberrypi5" in cfg
    assert "HostName 10.0.0.15" in cfg
    assert "User sre" in cfg
    assert "Host ubuntu-llm-01" in cfg
    assert "User aachten" in cfg
    assert "StrictHostKeyChecking no" in cfg


def test_fleet_ssh_config_skips_a_host_name_that_would_inject_a_directive():
    """Host names come from config keys. A newline or space would become a
    second ssh_config stanza; refuse rather than write it."""
    cfg = fleet_ssh_config({"pi\nHost evil": {"address": "10.0.0.1"}})
    assert "Host evil" not in cfg
    assert "10.0.0.1" not in cfg


def test_fleet_ssh_config_does_not_reuse_an_invalid_default_user():
    """MUTATION GUARD. Falling back to default_user after it failed _HOST_NAME
    re-injects the value that just failed — a newline becomes another Host."""
    cfg = fleet_ssh_config(
        {"pi": {"address": "10.0.0.1"}},
        default_user="sre\nHost evil",
    )
    assert "Host evil" not in cfg
    assert "User sre" in cfg
    assert "Host pi" in cfg


def test_load_identity_files_reads_the_secret_dir_not_the_developer_home(tmp_path, monkeypatch):
    """MUTATION GUARD. Scanning ~/.ssh would leak the developer's keys into a
    test Secret (and, in CI, fail closed because there is no such dir). The
    inventory key_path /keys/id_rsa does not exist on disk on purpose."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home" / ".ssh").mkdir(parents=True)
    (tmp_path / "home" / ".ssh" / "id_rsa").write_text("DEVELOPER KEY\n")
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "id_rsa").write_text("SESSION KEY\n")
    files = load_identity_files(str(secret), identity_paths_from_hosts(HOSTS))
    assert files["id_rsa"] == "SESSION KEY\n"
    assert "DEVELOPER KEY" not in "".join(files.values())


def test_load_identity_files_is_empty_when_nothing_on_disk_exists():
    """HOSTS in the ladder tests name /keys/id_rsa, which is not a real file.
    A test that forgot to plant a key must not silently pick up ~/.ssh."""
    assert load_identity_files("", identity_paths_from_hosts(HOSTS)) == {}


def test_a_kubernetes_ssh_privatekey_is_also_staged_as_id_rsa(tmp_path):
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "ssh-privatekey").write_text("FROM SECRET\n")
    files = load_identity_files(str(secret))
    assert files["ssh-privatekey"] == "FROM SECRET\n"
    assert files["id_rsa"] == "FROM SECRET\n"


def test_session_ssh_files_are_empty_without_a_key(tmp_path):
    assert session_ssh_files(HOSTS, secret_dir=str(tmp_path)) == {}


def test_session_ssh_files_include_config_and_the_key(tmp_path):
    (tmp_path / "id_rsa").write_text("KEY\n")
    files = session_ssh_files(HOSTS, default_user="sre", secret_dir=str(tmp_path))
    assert files["id_rsa"] == "KEY\n"
    assert "Host raspberrypi5" in files["config"]
    assert "IdentityFile ~/.ssh/id_rsa" in files["config"]


def test_session_ssh_files_use_absolute_identity_paths_on_the_host_tier(tmp_path):
    (tmp_path / "id_rsa").write_text("KEY\n")
    files = session_ssh_files(
        HOSTS, secret_dir=str(tmp_path),
        identity_dir="/tmp/cfop-cockpit-1889")
    assert "IdentityFile /tmp/cfop-cockpit-1889/id_rsa" in files["config"]
    assert "IdentityFile ~/.ssh/id_rsa" not in files["config"]


def test_the_bundle_round_trips_through_an_env_file():
    original = {"id_rsa": "-----BEGIN KEY-----\nline\n", "config": "Host pi\n"}
    blob = encode_ssh_bundle(original)
    assert "\n" not in blob
    assert decode_ssh_bundle(blob) == original
    assert SSH_BUNDLE_ENV == "CFOP_COCKPIT_SSH_BUNDLE"


def test_the_entrypoint_builds_an_in_cluster_kubeconfig_and_stages_ssh():
    """Cross-artifact seam: the image's entrypoint, not the agent's Python.
    Rename KUBERNETES_SERVICE_HOST or /ssh-secret here without the matching
    spawn change and a cockpit still has kubectl talking to localhost:8080."""
    text = (REPO_ROOT / "cockpit" / "entrypoint.sh").read_text()
    assert "KUBERNETES_SERVICE_HOST" in text
    assert ".kube/config" in text
    assert "tokenFile:" in text
    assert 'token: "%s"' not in text
    assert "/ssh-secret" in text
    assert SSH_BUNDLE_ENV in text
