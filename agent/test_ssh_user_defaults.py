"""No SSH username may be hardcoded anywhere in the tree (CFOP-137).

Six sites used to carry a default, split between two different guesses for the
same value -- `sre` in the agent and `aachten` in the executor, worker and
event_runtime. Two problems in one:

  * For node-actions the AGENT's value wins (it writes CFOP_SSH_USER into the
    Job env), so an unset config meant every node-action tried `sre@host` and
    failed authentication while the executor's own code said `aachten` two
    files away. Nothing caught it: valid config, healthy pod, failure only at
    connect time.
  * `aachten` is a personal account, and executor/ and worker/ ship in public
    images. A stranger installing this got a default pointing at this
    project's operator.

There is no defensible default SSH username for an arbitrary install, so the
resolution is to have none and refuse instead -- the posture CFOP-133 set for
the node-action allowlist.
"""

import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The two literals that were actually there. Deliberately narrow: this guards
# against these coming back, not against every string in the tree.
_BANNED = re.compile(r"""['"](?:aachten|sre)['"]""")

_FILES = [
    "agent/agent.py",
    "executor/nodeaction.py",
    "executor/entrypoint.py",
    "worker/entrypoint.py",
    "event_runtime/deep_investigation.py",
]


@pytest.mark.parametrize("relpath", _FILES)
def test_no_hardcoded_ssh_username(relpath):
    """A grep-shaped guard, like test_console_vendor.py's http(s):// check.

    Scoped to lines that actually concern the ssh user, so an unrelated
    occurrence of either word elsewhere does not fail the build.
    """
    path = os.path.join(_ROOT, relpath)
    offenders = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # comments explain the ban; they may name the values
            if not re.search(r"ssh_user|CFOP_SSH_USER|CFOP_DEEP_SSH_USER", line):
                continue
            if _BANNED.search(line):
                offenders.append(f"{relpath}:{n}: {stripped}")
    assert not offenders, (
        "hardcoded SSH username reintroduced — it must come from config:\n"
        + "\n".join(offenders))
