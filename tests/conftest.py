"""Suite-wide guards for ``tests/``.

CFOP-275: a cockpit test once replaced the developer's real ``~/.ssh/id_rsa``
with its fixture text, because the code under test stages an ssh identity into
``Path.home() / ".ssh"`` and a fake ssh runner isolates the network but not the
filesystem. Nothing in CI noticed — the runner's home has no key worth losing.

The fixture below does not prevent that write (the code fix does); it makes the
*class* of regression fail loudly, naming the test, on the first machine where
it would have mattered.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Tuple

import pytest

#: Captured at import, before any test can ``monkeypatch.setenv("HOME", ...)``.
#: A test that redirects HOME is still measured against the real directory,
#: because the real directory is the one a stray write would damage.
_REAL_SSH_DIR = Path(os.environ.get("HOME") or os.path.expanduser("~")) / ".ssh"


def _snapshot(directory: Path) -> Dict[str, Tuple[int, int]]:
    """``{name: (size, mtime_ns)}`` for the regular files directly in ``directory``.

    Not recursive and not content-hashed: cheap enough to run around every test,
    and an overwrite that keeps size and mtime identical is not a thing
    ``shutil.copyfile`` can do.
    """
    try:
        entries = list(directory.iterdir())
    except OSError:
        return {}
    out: Dict[str, Tuple[int, int]] = {}
    for entry in entries:
        try:
            if entry.is_file():
                st = entry.stat()
                out[entry.name] = (st.st_size, st.st_mtime_ns)
        except OSError:
            continue
    return out


#: Where the cockpit test helpers tell ``HostCockpitSpawner`` to stage the ssh
#: identity. Set per test, here rather than in ``test_cockpit_ladder.py``,
#: because ``test_cockpit_open.py`` and ``test_cockpit_fallback_host.py`` import
#: that module's ``spawner()`` / ``host_spawn()`` and a fixture defined there
#: would not be active for them.
COCKPIT_SSH_STAGING_ENV = "CFOP_TEST_COCKPIT_SSH_DIR"


@pytest.fixture(autouse=True)
def _cockpit_ssh_staging_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(COCKPIT_SSH_STAGING_ENV, str(tmp_path / "staged-ssh"))


@pytest.fixture(autouse=True)
def _the_real_ssh_dir_is_untouched(request):
    """Fail any test that adds, removes or rewrites a file in the real ``~/.ssh``."""
    before = _snapshot(_REAL_SSH_DIR)
    yield
    after = _snapshot(_REAL_SSH_DIR)
    if after == before:
        return
    changed = sorted(
        name for name in set(before) | set(after) if before.get(name) != after.get(name))
    pytest.fail(
        f"{request.node.nodeid} modified the real {_REAL_SSH_DIR} ({', '.join(changed)}). "
        f"Code under test must stage ssh material into a directory the test owns "
        f"(see cockpit.ladder.HostCockpitSpawner(ssh_dir=...), CFOP-275).",
        pytrace=False)
