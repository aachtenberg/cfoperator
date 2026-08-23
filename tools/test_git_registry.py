"""The tool half of the console repo registry (CFOP-77).

A repo linked or unlinked in the console changes what the model can reach, so
the tool registry has to be rebuildable while the process runs. The half that
would fail quietly is the *un*registration: repo names ride in the
``github_*`` schema descriptions, so an unlinked repo whose tools stay
registered is still being advertised to the model — the exact staleness the
console exists to remove.

ToolRegistry is built with ``__new__`` rather than its constructor: the real
one also dials up SSH, k8s, Timescale and Prometheus, none of which this
behaviour depends on.
"""

from types import SimpleNamespace

import pytest

from tools import ToolRegistry


REPOS = [
    {"name": "homelab-infra", "github": "aachtenberg/homelab-infra"},
    {"name": "esp-sensor-hub", "github": "aachtenberg/esp-sensor-hub", "path": "/srv/esp"},
]


def _registry(repos, *, token="ghp_test", monkeypatch=None):
    reg = ToolRegistry.__new__(ToolRegistry)
    reg.operator = SimpleNamespace(
        config={"git": {"repos": list(repos), "github": {"token": token}}})
    reg.tools = {}
    reg.github_tools = None
    reg.git_tools = None
    reg._init_git_tools()
    reg._register_git_tools()
    return reg


def _names(reg):
    return {name for name in reg.tools if name.startswith(("git_", "github_"))}


def test_linked_repos_produce_tools_that_name_them():
    reg = _registry(REPOS)
    assert _names(reg)
    described = " ".join(
        entry["schema"]["description"] for entry in reg.tools.values())
    assert "esp-sensor-hub" in described


def test_unlinking_a_repo_updates_what_the_tools_advertise():
    reg = _registry(REPOS)
    reg.operator.config["git"]["repos"] = [REPOS[0]]
    reg.refresh_git_tools()
    described = " ".join(
        entry["schema"]["description"] for entry in reg.tools.values())
    assert "homelab-infra" in described
    assert "esp-sensor-hub" not in described


def test_unlinking_the_last_repo_takes_the_tools_away():
    """The guard this file exists for. Rebuilding without unregistering first
    leaves every github_* tool in place, pointed at repos that are gone."""
    reg = _registry(REPOS)
    assert _names(reg)
    reg.operator.config["git"]["repos"] = []
    reg.refresh_git_tools()
    assert _names(reg) == set()
    assert reg.github_tools is None
    assert reg.git_tools is None


def test_linking_the_first_repo_brings_the_tools_up_without_a_restart():
    reg = _registry([])
    assert _names(reg) == set()
    reg.operator.config["git"]["repos"] = list(REPOS)
    reg.refresh_git_tools()
    assert _names(reg)
    assert reg.github_tools is not None


def test_local_git_tools_track_whether_any_repo_has_a_clone():
    reg = _registry(REPOS)
    assert reg.git_tools is not None, "esp-sensor-hub has a path"
    reg.operator.config["git"]["repos"] = [REPOS[0]]  # no path
    reg.refresh_git_tools()
    assert reg.git_tools is None
    assert reg.github_tools is not None, "the API path does not need a clone"


def test_refresh_leaves_unrelated_tools_alone():
    reg = _registry(REPOS)
    reg.tools["prometheus_query"] = {"function": None, "schema": {"name": "prometheus_query"}}
    reg.operator.config["git"]["repos"] = []
    reg.refresh_git_tools()
    assert "prometheus_query" in reg.tools


def test_a_token_only_in_the_environment_still_arms_the_github_tools(monkeypatch):
    """The Helm ConfigMap templates no git block at all, so a repo linked from
    the console there has no config-side token to find."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_from_env")
    reg = _registry(REPOS, token="")
    assert reg.github_tools is not None


def test_no_token_anywhere_leaves_the_github_tools_off(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    reg = _registry(REPOS, token="")
    assert reg.github_tools is None
    assert not any(name.startswith("github_") for name in reg.tools)
