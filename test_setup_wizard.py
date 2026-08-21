"""Guards for `cfoperator init` (setup_wizard.py, CFOP-45).

The class of regression, not today's output:

  1. **The wizard must have no field list of its own.** Its interview is
     generated from the alias tables in `cfshared.config`; the guard extends
     the schema at test time and asserts the wizard surfaces the new entry —
     in the plan, and end-to-end into a written file the real loader resolves
     — without `setup_wizard.py` being edited.
  2. **The written file must mean what the answers said.** Checked through
     `cfshared.config.load_config`, the loader every deployment actually
     runs, never through a re-implementation of it.
  3. **A wrong answer fails naming its section**, interactively and in
     `--non-interactive`, because "rejected during setup, naming what failed"
     is the issue's whole value proposition.
  4. **Re-running against a good config is read-only.** Validate-only must
     leave both files byte-identical.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

import setup_wizard as wiz
from cfshared import config as cfg

REPO = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Stub endpoints: one server answers as ollama, prometheus, alertmanager and
# loki at once — the probe paths are disjoint, so the URLs may coincide.
# ---------------------------------------------------------------------------


class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: ARG002 - silence request logging
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        base = self.server.base_url
        if self.path == "/api/tags":
            self._json({"models": [{"name": "llama3.1:8b"}, {"name": "nomic-embed-text"}]})
        elif self.path == "/api/v1/targets":
            self._json({"data": {"activeTargets": [
                {"labels": {"job": "node"}, "health": "up", "scrapeUrl": f"{base}/metrics"},
                {"labels": {"job": "node"}, "health": "down", "scrapeUrl": f"{base}/metrics"},
                {"labels": {"job": "loki"}, "health": "up", "scrapeUrl": f"{base}/metrics"},
            ]}})
        elif self.path == "/api/v1/alertmanagers":
            self._json({"data": {"activeAlertmanagers": [{"url": f"{base}/api/v2/alerts"}]}})
        elif self.path == "/api/v2/status":
            self._json({"versionInfo": {"version": "0.27.0"}})
        elif self.path == "/loki/api/v1/labels":
            self._json({"status": "success", "data": ["job", "namespace"]})
        else:
            self._json({"error": "not found"}, status=404)


@pytest.fixture()
def stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.base_url
    server.shutdown()


def _dead_url() -> str:
    """A URL nothing listens on (bind, learn the port, close)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


def _noninteractive_env(monkeypatch, stub_url, **extra):
    for name in ("OLLAMA_URL", "PROMETHEUS_URL", "ALERTMANAGER_URL", "LOKI_URL"):
        monkeypatch.setenv(name, stub_url)
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.1:8b")
    for name, value in extra.items():
        monkeypatch.setenv(name, value)


# ---------------------------------------------------------------------------
# 1. No independent field list: extending the schema extends the wizard.
# ---------------------------------------------------------------------------


def _backend_specs(plan):
    return [spec for spec in plan if spec["kind"] == "backend"]


def test_plan_is_generated_from_the_backend_alias_table(monkeypatch):
    """A backend alias added to the schema appears in the plan — appended,
    optional, generically probed — with no edit to setup_wizard.py."""
    baseline = {spec["alias"] for spec in _backend_specs(wiz.build_plan())}
    assert baseline == set(cfg._BACKEND_ALIASES)

    monkeypatch.setattr(cfg, "_BACKEND_ALIASES",
                        {**cfg._BACKEND_ALIASES, "tempo": ("traces", "tempo")})
    specs = _backend_specs(wiz.build_plan())
    tempo = next(spec for spec in specs if spec["alias"] == "tempo")
    assert tempo["required"] is False, "an unknown new section must never hard-block the interview"
    assert tempo["env_var"] == "TEMPO_URL"
    assert specs[-1] is tempo, "unknown sections are appended after the ordered ones"


def test_plan_is_generated_from_the_notify_alias_table(monkeypatch):
    monkeypatch.setattr(cfg, "_NOTIFY_ALIASES", cfg._NOTIFY_ALIASES + (
        ("pagerduty_key", "pagerduty", {"pagerduty_key": "routing_key"}),
    ))
    notify = next(spec for spec in wiz.build_plan() if spec["kind"] == "notify")
    sink = next(s for s in notify["sinks"] if s["alias"] == "pagerduty_key")
    assert sink["backend"] == "pagerduty"
    assert sink["keys"] == {"pagerduty_key": "routing_key"}


def test_new_llm_alias_key_is_surfaced_as_edit_the_file(monkeypatch):
    """An llm key the wizard does not prompt for still shows up — in the
    summary's edit-the-file list — the moment the schema learns it."""
    monkeypatch.setattr(cfg, "_LLM_ALIASES",
                        {**cfg._LLM_ALIASES, "triage_model": "triage_model"})
    llm = next(spec for spec in wiz.build_plan() if spec["kind"] == "llm")
    assert "triage_model" in llm["unasked"]
    assert "timeout" in llm["unasked"], "keys the wizard skips today must already be listed"


def test_schema_added_backend_flows_end_to_end(monkeypatch, tmp_path, stub):
    """The Done-when, verbatim: adding a field to the schema surfaces it
    without editing the wizard — all the way into a file the loader resolves."""
    monkeypatch.setattr(cfg, "_BACKEND_ALIASES",
                        {**cfg._BACKEND_ALIASES, "tempo": ("traces", "tempo")})
    _noninteractive_env(monkeypatch, stub, TEMPO_URL=stub)

    assert wiz.main(["--non-interactive", "--dir", str(tmp_path)]) == 0
    resolved = cfg.load_config(str(tmp_path / "config.yaml"))
    assert resolved["observability"]["traces"]["url"] == stub


# ---------------------------------------------------------------------------
# 2. The written files mean what the answers said — per the real loader.
# ---------------------------------------------------------------------------


def test_non_interactive_run_writes_files_the_loader_resolves(monkeypatch, tmp_path, stub):
    _noninteractive_env(monkeypatch, stub, SLACK_WEBHOOK_URL="https://hooks.example.invalid/T00/B00")

    assert wiz.main(["--non-interactive", "--dir", str(tmp_path)]) == 0

    resolved = cfg.load_config(str(tmp_path / "config.yaml"))
    assert resolved["observability"]["metrics"]["url"] == stub
    assert resolved["observability"]["alerts"]["url"] == stub
    assert resolved["observability"]["logs"]["url"] == stub
    assert resolved["llm"]["primary"]["provider"] == "ollama"
    assert resolved["llm"]["primary"]["url"] == stub
    assert resolved["llm"]["primary"]["model"] == "llama3.1:8b"
    assert resolved["profile"] == "investigate"
    # The notify value must resolve through ${SLACK_WEBHOOK_URL}, not be inlined.
    assert "hooks.example.invalid" not in (tmp_path / "config.yaml").read_text()
    sinks = resolved["observability"]["notifications"]
    assert [s["backend"] for s in sinks] == ["slack"]
    assert sinks[0]["webhook_url"] == "https://hooks.example.invalid/T00/B00"

    env_text = (tmp_path / ".env").read_text()
    env_map = dict(line.split("=", 1) for line in env_text.splitlines()
                   if line and not line.startswith("#"))
    assert env_map["SLACK_WEBHOOK_URL"] == "https://hooks.example.invalid/T00/B00"
    assert env_map["PROMETHEUS_URL"] == stub
    assert env_map["CFOP_ADMIN_USERNAME"] == "admin"
    assert env_map["CFOP_ADMIN_PASSWORD"], "an admin password must be generated when none is given"


def test_emitted_keys_stay_inside_the_schema(monkeypatch, tmp_path, stub):
    """Whatever the wizard writes must fold into keys the loader knows.
    An unknown key would be silently carried, configuring nothing."""
    _noninteractive_env(monkeypatch, stub)
    assert wiz.main(["--non-interactive", "--dir", str(tmp_path)]) == 0
    raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert set(cfg.normalize_aliases(raw)) <= set(cfg.DEFAULT_CONFIG)


# ---------------------------------------------------------------------------
# 3. Wrong answers fail during setup, naming what failed.
# ---------------------------------------------------------------------------


def test_probe_failure_names_the_section(monkeypatch, tmp_path, stub, capsys):
    _noninteractive_env(monkeypatch, stub)
    monkeypatch.setenv("PROMETHEUS_URL", _dead_url())

    assert wiz.main(["--non-interactive", "--dir", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "prometheus" in err
    assert not (tmp_path / "config.yaml").exists(), "a failed run must write nothing"


def test_unpulled_model_is_rejected_by_name(monkeypatch, tmp_path, stub, capsys):
    _noninteractive_env(monkeypatch, stub)
    monkeypatch.setenv("OLLAMA_MODEL", "not-pulled:1b")

    assert wiz.main(["--non-interactive", "--dir", str(tmp_path)]) == 1
    assert "not-pulled:1b" in capsys.readouterr().err


def test_missing_required_values_are_reported_together(monkeypatch, tmp_path, capsys):
    for name in ("OLLAMA_URL", "OLLAMA_MODEL", "PROMETHEUS_URL"):
        monkeypatch.delenv(name, raising=False)

    assert wiz.main(["--non-interactive", "--dir", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    for name in ("OLLAMA_URL", "OLLAMA_MODEL", "PROMETHEUS_URL"):
        assert name in err, "CI should learn every gap in one failure, not one per run"


# ---------------------------------------------------------------------------
# 4. Re-running is safe: refuse to clobber, and validate-only writes nothing.
# ---------------------------------------------------------------------------


def test_existing_config_is_not_clobbered_without_force(monkeypatch, tmp_path, stub, capsys):
    _noninteractive_env(monkeypatch, stub)
    (tmp_path / "config.yaml").write_text("profile: investigate\n")

    assert wiz.main(["--non-interactive", "--dir", str(tmp_path)]) == 1
    assert "--force" in capsys.readouterr().err
    assert (tmp_path / "config.yaml").read_text() == "profile: investigate\n"


def test_validate_only_reports_healthy_and_changes_nothing(monkeypatch, tmp_path, stub, capsys):
    _noninteractive_env(monkeypatch, stub)
    assert wiz.main(["--non-interactive", "--dir", str(tmp_path)]) == 0
    before = {name: (tmp_path / name).read_bytes() for name in ("config.yaml", ".env")}
    capsys.readouterr()

    assert wiz.main(["--validate-only", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "healthy" in out
    assert "nothing was written" in out
    for name, content in before.items():
        assert (tmp_path / name).read_bytes() == content


def test_validate_only_fails_naming_the_dead_section(tmp_path, capsys):
    """Hand-written configs are first-class: validate-only probes them the
    same way, and a dead endpoint fails by name."""
    (tmp_path / "config.yaml").write_text(
        f"profile: investigate\nprometheus:\n  url: {_dead_url()}\n")

    assert wiz.main(["--validate-only", "--dir", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "prometheus" in out


# ---------------------------------------------------------------------------
# 5. Nothing destructive happens by accident.
# ---------------------------------------------------------------------------


def test_notify_test_post_does_not_fire_on_a_bare_enter(monkeypatch):
    """Enter is what a first-time operator presses through an unfamiliar
    wizard. The test post goes to a live on-call channel, so opting in must be
    an act, not the absence of one — the sink is still configured either way."""
    notify = next(spec for spec in wiz.build_plan() if spec["kind"] == "notify")
    answers = iter(["https://hooks.example.invalid/T00/B00"])
    monkeypatch.setattr(wiz, "_ask", lambda *a, **k: next(answers, ""))
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")  # bare Enter at every confirm

    fired = []
    monkeypatch.setattr(wiz, "probe_notify",
                        lambda backend, values: (fired.append(backend), (True, "sent"))[1])

    collected = wiz._collect_notify_interactive(notify)
    assert fired == [], "a bare Enter must not post to a live channel"
    assert collected["slack_webhook"] == "https://hooks.example.invalid/T00/B00", \
        "declining the test post must still keep the sink"


def test_failed_verification_leaves_no_files_behind(monkeypatch, tmp_path, stub, capsys):
    """A config the summary never described must not survive the run — it
    would also make the next --non-interactive run refuse without --force."""
    _noninteractive_env(monkeypatch, stub)
    monkeypatch.setattr(wiz, "verify_written",
                        lambda *a, **k: (_ for _ in ()).throw(wiz.WizardError("simulated mismatch")))

    assert wiz.main(["--non-interactive", "--dir", str(tmp_path)]) == 1
    assert "simulated mismatch" in capsys.readouterr().err
    assert sorted(p.name for p in tmp_path.iterdir()) == [], \
        "a failed verification must leave neither the outputs nor staging files"


def test_failed_verification_leaves_an_existing_config_untouched(monkeypatch, tmp_path, stub):
    """Regeneration is only committed once it verifies; a half-written
    overwrite is worse than the old config that at least booted."""
    _noninteractive_env(monkeypatch, stub)
    (tmp_path / "config.yaml").write_text("profile: remediate\n")
    (tmp_path / ".env").write_text("PROMETHEUS_URL=http://old.invalid\n")
    monkeypatch.setattr(wiz, "verify_written",
                        lambda *a, **k: (_ for _ in ()).throw(wiz.WizardError("simulated mismatch")))

    assert wiz.main(["--non-interactive", "--force", "--dir", str(tmp_path)]) == 1
    assert (tmp_path / "config.yaml").read_text() == "profile: remediate\n"
    assert (tmp_path / ".env").read_text() == "PROMETHEUS_URL=http://old.invalid\n"
    assert not list(tmp_path.glob("*.tmp"))


def test_env_written_is_not_world_readable(monkeypatch, tmp_path, stub):
    """It holds the admin password and every webhook."""
    _noninteractive_env(monkeypatch, stub)
    assert wiz.main(["--non-interactive", "--dir", str(tmp_path)]) == 0
    assert (tmp_path / ".env").stat().st_mode & 0o077 == 0


def test_non_interactive_knobs_are_documented(monkeypatch, tmp_path, stub):
    """`--non-interactive` promises the env names .env.example documents.

    Guards the class rather than today's three names: any CFOP_INIT_* the
    collector learns to read must be documented, or CI copy-pastes the example
    and silently gets a default it never chose.
    """
    documented = (REPO / ".env.example").read_text()
    read_by_wizard = set(re.findall(r"CFOP_INIT_[A-Z_]+", (REPO / "setup_wizard.py").read_text()))
    assert read_by_wizard, "the pattern stopped matching — reread the collector"
    undocumented = sorted(name for name in read_by_wizard if name not in documented)
    assert not undocumented, f"read by --non-interactive but absent from .env.example: {undocumented}"


def test_documented_profile_knob_actually_drives_the_run(monkeypatch, tmp_path, stub):
    """The other half of the same claim: a documented name must do something."""
    _noninteractive_env(monkeypatch, stub, CFOP_INIT_PROFILE="remediate")
    assert wiz.main(["--non-interactive", "--dir", str(tmp_path)]) == 0
    assert cfg.load_config(str(tmp_path / "config.yaml"))["profile"] == "remediate"


# ---------------------------------------------------------------------------
# The entry point itself
# ---------------------------------------------------------------------------


def test_cfoperator_wrapper_dispatches_init():
    result = subprocess.run(
        [sys.executable, str(REPO / "cfoperator"), "init", "--help"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0
    assert "config.yaml" in result.stdout


def test_cfoperator_wrapper_rejects_unknown_verbs():
    result = subprocess.run(
        [sys.executable, str(REPO / "cfoperator"), "frobnicate"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2
    assert "frobnicate" in result.stderr
