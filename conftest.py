"""Top-level pytest conftest.

Sole job: keep the test suite from ever paging production Slack/Discord.

Several tests fire real alerts through ``build_portable_runtime()``, which
walks repo-root ``.env`` via ``os.environ.setdefault``. A real webhook URL
in that ``.env`` (the homelab cluster has one) gets pulled into the
process env, sinks register against it, and a test alert like
"portable run" lands in the on-call channel.

We neutralize the failure mode globally:

  - Set the webhook env vars to empty strings (the sinks short-circuit
    when ``webhook_url`` is falsy).
  - Set them to "" rather than deleting — ``_load_env_file`` uses
    ``os.environ.setdefault``, which respects an existing empty string
    but happily repopulates a deleted key from ``.env``.
  - Drop ``CONFIG_PATH`` so bootstrap does not load the repo-root config
    by default (individual tests pass their own ``config_path=`` when
    they need one).

Per-test ``monkeypatch.setenv(...)`` still wins, so tests that
intentionally configure a fake webhook (e.g. assertions about sink
registration) continue to work unchanged.
"""

import os


os.environ["SLACK_WEBHOOK_URL"] = ""
os.environ["DISCORD_WEBHOOK_URL"] = ""
os.environ.pop("CONFIG_PATH", None)
