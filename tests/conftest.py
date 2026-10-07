"""Suite-wide fixtures for ``tests/``.

The guard that fails any test touching the real ``~/.ssh`` lives in the
repo-root ``conftest.py``, which every per-directory invocation loads; this
file holds what only ``tests/`` needs.
"""

from __future__ import annotations

import pytest

#: Where the cockpit test helpers tell ``HostCockpitSpawner`` to stage the ssh
#: identity (CFOP-275). Set per test, here rather than in
#: ``test_cockpit_ladder.py``, because ``test_cockpit_open.py`` and
#: ``test_cockpit_fallback_host.py`` import that module's ``spawner()`` /
#: ``host_spawn()`` and a fixture defined there would not be active for them.
COCKPIT_SSH_STAGING_ENV = "CFOP_TEST_COCKPIT_SSH_DIR"


@pytest.fixture(autouse=True)
def _cockpit_ssh_staging_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(COCKPIT_SSH_STAGING_ENV, str(tmp_path / "staged-ssh"))
