"""Agent-side half of the CFOP-26 config guards.

The bulk of the merge semantics is tested in the root-level `test_config_merge.py`.
What has to live here is anything that imports `agent.agent`, because that module
uses bare imports (`node_action_plan`, `ollama_pool`, …) that only resolve with
`agent/` on sys.path — which is exactly what this suite provides and the
root-level suite deliberately does not.
"""

from __future__ import annotations

import textwrap

import pytest

from cfshared import config as shared_config


@pytest.fixture(scope="module")
def CFOperator():
    from agent.agent import CFOperator as _CFOperator

    return _CFOperator


def _bare(CFOperator):
    """An instance with no __init__ run — the pattern the other agent tests use."""
    return object.__new__(CFOperator)


def _write(tmp_path, text: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(path)


def test_load_config_delegates_to_the_shared_loader(tmp_path, CFOperator):
    """The agent and the event runtime must not be able to disagree about a file."""
    path = _write(tmp_path, """
        prometheus:
          url: http://prom.example:9090
        profile: investigate
    """)
    assert _bare(CFOperator)._load_config(path) == shared_config.load_config(path)


def test_default_config_is_the_shared_schema(CFOperator):
    """`_default_config()` used to be a second, incomplete opinion.

    Most visibly it had no `llm` section at all, so an agent started without a
    config file had no model endpoint to call.
    """
    defaults = _bare(CFOperator)._default_config()
    assert defaults == shared_config.DEFAULT_CONFIG
    assert defaults["llm"]["primary"]["provider"] == "ollama"


def test_a_minimal_config_still_yields_a_database_block(tmp_path, CFOperator):
    """__init__ builds the DB URL by direct indexing; a missing section was a
    bare KeyError before any of the diagnostics had a chance to run."""
    config = _bare(CFOperator)._load_config(_write(tmp_path, "prometheus:\n  url: http://p:9090\n"))
    url = (
        f"postgresql://{config['database']['user']}:{config['database']['password']}"
        f"@{config['database']['host']}:{config['database']['port']}/{config['database']['database']}"
    )
    assert url.startswith("postgresql://cfoperator:")


class _FakeKB:
    """Stands in for the knowledge base's live settings store."""

    def __init__(self, settings=None):
        self.settings = settings or {}

    def get_setting(self, key, default=""):
        return self.settings.get(key, default)


def _operator_with(CFOperator, config, settings=None):
    operator = _bare(CFOperator)
    operator.config = config
    operator.kb = _FakeKB(settings)
    return operator


@pytest.mark.parametrize("flag", shared_config.REMEDIATION_FLAGS)
def test_investigate_profile_beats_the_live_db_override(CFOperator, flag):
    """The console can toggle these flags live, so the config-side clamp alone
    would leave the console as a way to escalate past the profile."""
    operator = _operator_with(
        CFOperator,
        {"profile": shared_config.PROFILE_INVESTIGATE, "remediation": {flag: True}},
        settings={f"remediation_{flag}": "true"},
    )
    assert operator._remediation_flag(flag) is False


@pytest.mark.parametrize("flag", shared_config.REMEDIATION_FLAGS)
def test_remediate_profile_honours_the_live_db_override(CFOperator, flag):
    operator = _operator_with(
        CFOperator,
        {"profile": shared_config.PROFILE_REMEDIATE, "remediation": {flag: False}},
        settings={f"remediation_{flag}": "true"},
    )
    assert operator._remediation_flag(flag) is True


def test_unprofiled_config_keeps_pre_cfop26_flag_behaviour(CFOperator):
    """Production has no `profile:` key and auto-deploys on merge."""
    operator = _operator_with(CFOperator, {"remediation": {"queue_drain": True}})
    assert operator._remediation_flag("queue_drain") is True

    operator = _operator_with(
        CFOperator,
        {"remediation": {"queue_drain": False}},
        settings={"remediation_queue_drain": "true"},
    )
    assert operator._remediation_flag("queue_drain") is True


def test_infra_summary_without_hosts_is_a_state_not_an_empty_header(CFOperator):
    """A bare "Infrastructure hosts:" header reads to the model as "there are
    none", which is wrong — they are discovered rather than declared."""
    operator = _bare(CFOperator)
    operator.config = shared_config.default_config()
    operator.containers = None
    summary = operator._get_infra_summary()
    assert "not statically configured" in summary
    assert not summary.startswith("Infrastructure hosts:\n\n")
