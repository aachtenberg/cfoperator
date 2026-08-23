"""Agent-side resolution of the linked repo registry (CFOP-77).

``config['git']['repos']`` stays the one place every consumer reads the
registry from — the tool registry, the remediation proposer — so what changed
is that its contents are *resolved* (DB setting over config file) rather than
copied out of the YAML. Two things about that are easy to get wrong and
invisible when wrong:

* the file's own list has to be kept separately, or the console has nothing to
  show for "what is config.yaml still saying" and reverting has nothing to
  revert to;
* a database that cannot be read must leave the file's repos linked, not
  unlink everything.

Lives here rather than in the root suite because it imports ``agent.agent``,
which needs ``agent/`` on sys.path (see test_config_loader.py).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cfshared import repos as shared_repos


FILE_REPOS = [{"name": "homelab-infra", "github": "aachtenberg/homelab-infra", "branch": "main"}]
DB_REPOS = [{"name": "esp-sensor-hub", "github": "aachtenberg/esp-sensor-hub"}]


@pytest.fixture(scope="module")
def CFOperator():
    from agent.agent import CFOperator as _CFOperator

    return _CFOperator


def _operator(CFOperator, *, stored="", kb_raises=False):
    op = CFOperator.__new__(CFOperator)
    op.config = {"git": {"repos": [dict(r) for r in FILE_REPOS], "github": {}}}

    def get_setting(key, default=None):
        if kb_raises:
            raise ConnectionError("Database is offline")
        return stored if key == shared_repos.SETTING_KEY else default

    op.kb = SimpleNamespace(get_setting=get_setting)
    op.tools = SimpleNamespace(refresh_git_tools=lambda: refreshes.append(1))
    refreshes: list[int] = []
    op._test_refreshes = refreshes
    return op


def test_the_stored_registry_becomes_what_every_consumer_reads(CFOperator):
    op = _operator(CFOperator, stored=shared_repos.dumps(DB_REPOS))
    op._load_git_registry()
    assert [r["name"] for r in op.git_repos()] == ["esp-sensor-hub"]
    assert op.config["git"]["repos"] == op.git_repos()


def test_the_files_own_list_is_kept_alongside_the_effective_one(CFOperator):
    """Overwriting config['git']['repos'] in place is what makes every existing
    consumer work unchanged; it also means the file's list has to be stashed
    before the overwrite or it is gone."""
    op = _operator(CFOperator, stored=shared_repos.dumps(DB_REPOS))
    op._load_git_registry()
    assert [r["name"] for r in op.file_git_repos()] == ["homelab-infra"]
    assert [r["name"] for r in op.git_repos()] == ["esp-sensor-hub"]


def test_reverting_puts_the_files_list_back(CFOperator):
    op = _operator(CFOperator, stored=shared_repos.dumps(DB_REPOS))
    op._load_git_registry()
    op.apply_git_repos(None)
    assert [r["name"] for r in op.git_repos()] == ["homelab-infra"]
    assert op._git_repos_source == "config"


def test_an_unreadable_database_leaves_the_files_repos_linked(CFOperator):
    """The failure mode this replaces is worse than a stale list: an agent
    that starts with no repos investigates blind and says nothing about it."""
    op = _operator(CFOperator, kb_raises=True)
    op._load_git_registry()
    assert [r["name"] for r in op.git_repos()] == ["homelab-infra"]
    assert op._git_repos_source == "config"


def test_applying_a_list_rebuilds_the_tools(CFOperator):
    """Without this the console's write is persisted but the running process
    keeps the old tools — and their schemas still name the old repos."""
    op = _operator(CFOperator)
    op._load_git_registry()
    op.apply_git_repos(DB_REPOS)
    assert op._test_refreshes, "apply_git_repos must refresh the tool registry"
    assert [r["name"] for r in op.git_repos()] == ["esp-sensor-hub"]


def test_a_tool_refresh_failure_does_not_lose_the_new_list(CFOperator):
    op = _operator(CFOperator)
    op._load_git_registry()

    def boom():
        raise RuntimeError("tool registry is wedged")

    op.tools = SimpleNamespace(refresh_git_tools=boom)
    op.apply_git_repos(DB_REPOS)
    assert [r["name"] for r in op.git_repos()] == ["esp-sensor-hub"]
