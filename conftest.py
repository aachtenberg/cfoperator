"""Top-level pytest conftest.

Sole job: the suite must not inherit the machine it runs on. Two things
leak in if nothing stops them — the developer's ``.env`` and the
developer's ``config.yaml`` — and both have caused real damage:

  - Tests that fire alerts through ``build_portable_runtime()`` registered
    sinks against a real webhook out of ``.env``, so a test alert like
    "portable run" landed in the on-call channel.
  - A failing config assertion rendered resolved values into its message,
    and those values were four live API keys, the database password and a
    GitHub PAT (CFOP-44). Anyone pasting that output into an issue, a PR
    or a chat disclosed production credentials.

Three controls, in order of how much they carry:

  - ``CFOP_NO_DOTENV`` — the load-bearing one. ``.env`` is not read at
    all, by ``cfshared.config.load_env_file`` or by ``agent.main()``. A
    kill switch rather than a list of variables to blank, because the
    list *was* the bug: the two webhook entries below were correct and
    the file still leaked six credentials once ``.env`` grew past them.
    Anything keyed on variable names rots the same way.
  - ``CONFIG_PATH`` points at an **empty file**. It used to be deleted,
    which worked while an unset value meant "no config"; the shared
    loader now falls back to ``config.yaml`` relative to the cwd, so
    deleting it resolves to the operator's live site config instead. An
    empty file also keeps ``file_present`` true, so the profile stays
    *unprofiled* rather than becoming ``investigate``.
  - Blanking the webhook variables — now defence in depth, since the kill
    switch already stops the ``.env`` path. It still covers a webhook
    exported directly into the shell. Set to ``""`` rather than deleted:
    ``load_env_file`` uses ``os.environ.setdefault``, which respects an
    existing empty string but repopulates a deleted key.

Per-test ``monkeypatch.setenv(...)`` still wins, so tests that
intentionally configure a fake webhook continue to work unchanged. Tests
that are *about* ``.env`` resolution opt back in with
``monkeypatch.delenv("CFOP_NO_DOTENV")`` — and must ``chdir`` to a tmp
dir when they do, because ``load_env_file`` also walks ``Path.cwd()``.

The same principle runs the other way — the suite must not *write to* the
machine either — and that is the fixture at the bottom. Three places stage
a mounted ssh secret into ``Path.home() / ".ssh"`` so ssh finds it as a
default identity (``cockpit.ladder``, ``worker.entrypoint``,
``executor.nodeaction``). A test that reaches one of them with a fake ssh
runner isolates the network but not that write; a cockpit test did, and
replaced the developer's real ``~/.ssh/id_rsa`` with "SESSION KEY"
(CFOP-275). The fixture does not prevent the write — the code fix does —
it makes the *class* of regression fail loudly, by test name, on the first
machine where it would have mattered. It lives here rather than in
``tests/`` because every per-directory invocation loads this file.
"""

import atexit
import os
import tempfile
from pathlib import Path
from typing import Dict, Tuple

import pytest


# Do not read the developer's .env at all. Blanking named variables (as the
# two lines below do) was the original approach and it rotted: .env grew to
# ~20 keys while the list stayed at two, and a failing config assertion in
# test_config_merge.py rendered four live API keys, the database password and
# a GitHub token into its output (CFOP-44).
#
# Keyed on the mechanism rather than on variable names, so a key added to .env
# tomorrow cannot leak by being unlisted. cfshared.config.load_env_file and
# agent.main() both honour it.
os.environ["CFOP_NO_DOTENV"] = "1"

# Kept even though the switch above makes them redundant for the .env path:
# these also neutralise a webhook exported directly into the shell, which is
# how someone would most plausibly page the on-call channel from a test run.
os.environ["SLACK_WEBHOOK_URL"] = ""
os.environ["DISCORD_WEBHOOK_URL"] = ""

# Same reasoning for a plugin list exported into the shell: every test that
# builds the runtime would import that plugin and run its registration, which
# for a real integration means real credentials and real polling (CFOP-208).
os.environ["CFOP_EVENT_RUNTIME_PLUGINS"] = ""

# Point config resolution at an empty file instead of dropping CONFIG_PATH.
#
# Dropping it used to be enough: the old loader returned {} when CONFIG_PATH
# was unset. cfshared.config.load_config now falls back to the literal
# "config.yaml", so an unset variable resolves to the *developer's live site
# config* in the repo root — which is how three tests came to read a real
# git.repos map and a real ntfy sink. An empty file gives the same "no ambient
# config" the pop was written for, and keeps `file_present` true so the
# profile still resolves to unprofiled rather than to `investigate`.
_EMPTY_CONFIG = tempfile.NamedTemporaryFile(  # noqa: SIM115 - lives for the session
    prefix="cfop-hermetic-config-", suffix=".yaml", delete=False
)
_EMPTY_CONFIG.close()
os.environ["CONFIG_PATH"] = _EMPTY_CONFIG.name
atexit.register(lambda: os.path.exists(_EMPTY_CONFIG.name) and os.unlink(_EMPTY_CONFIG.name))


# ---------------------------------------------------------------------------
# The suite must not write to the machine either (CFOP-275).
# ---------------------------------------------------------------------------

#: Captured at import, before any test can ``monkeypatch.setenv("HOME", ...)``.
#: A test that redirects HOME is still measured against the real directory,
#: because the real directory is the one a stray write would damage.
_REAL_SSH_DIR = Path(os.environ.get("HOME") or os.path.expanduser("~")) / ".ssh"


def _snapshot_ssh_dir(directory: Path) -> Dict[str, Tuple[int, int]]:
    """``{name: (size, mtime_ns)}`` for the regular files directly in ``directory``.

    Shallow on purpose: regular files (and symlinks to them) directly in the
    directory, no subdirectories, no content hash. That is cheap enough to run
    around every test, it is where ssh looks for a default identity, and an
    overwrite that keeps size and mtime identical is not a thing
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


@pytest.fixture(autouse=True)
def _the_real_ssh_dir_is_untouched(request):
    """Fail any test that adds, removes or rewrites a file in the real ``~/.ssh``."""
    before = _snapshot_ssh_dir(_REAL_SSH_DIR)
    yield
    after = _snapshot_ssh_dir(_REAL_SSH_DIR)
    if after == before:
        return
    changed = sorted(
        name for name in set(before) | set(after) if before.get(name) != after.get(name))
    pytest.fail(
        f"{request.node.nodeid} modified the real {_REAL_SSH_DIR} ({', '.join(changed)}). "
        f"Code under test must stage ssh material into a directory the test owns "
        f"(see cockpit.ladder.HostCockpitSpawner(ssh_dir=...), CFOP-275).",
        pytrace=False)
