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
"""

import atexit
import os
import tempfile


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
