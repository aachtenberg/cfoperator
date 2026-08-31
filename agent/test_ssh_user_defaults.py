"""No maintainer username may appear as an SSH user anywhere (CFOP-137).

Two different things were tangled together before this test existed, and the
review on #225 was right that only one of them is an invariant:

  * **The maintainer's account must never be a default or an example.**
    ``executor/`` and ``worker/`` ship in public images and ``docs/`` is a
    public reference, so a default of ``aachten`` pointed strangers at this
    project's operator. That is the regression this guards, and it is guarded
    across the WHOLE TREE rather than a hand-kept file list -- the first
    version of this test listed five files and missed ``cockpit_ladder.py``,
    ``observability/prometheus_containers.py``, the Helm values and
    ``docs/config-reference.md``, which is exactly how a list-based guard
    fails.

  * **A generic placeholder default is fine, and is a deliberate decision.**
    ``PrometheusContainers`` documents it: pin ``sre`` so changing the
    documented placeholder is a conscious edit, and assert it is not the
    maintainer's name. That decision stands; this test does not fight it.

What DOES have no default is any path that spends something on a wrong guess:
the executor's SSH run, the deep-investigation worker's billed prompt, and the
Job spawn that costs a budget slot. Those refuse and name the setting, and are
guarded in their own suites.
"""

import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The maintainer's account. Not a secret — the point is that a PUBLIC artifact
# must not steer an unconfigured install at a specific person's login.
_MAINTAINER = "aachten"

_SCAN_EXT = (".py", ".yaml", ".yml", ".md")
_SKIP_DIRS = {".git", ".claude", "__pycache__", "node_modules", ".venv", "ui/vendor"}

# A line that both mentions an ssh user AND names the maintainer. Scoped so an
# unrelated "aachtenberg/homelab-infra" repo path (which is legitimate and
# everywhere) does not trip it: the repo owner is `aachtenberg`, the login is
# `aachten`, so the word boundary matters.
_SSH_USER_LINE = re.compile(r"ssh[_-]?user", re.I)
_MAINTAINER_LITERAL = re.compile(rf"""(?<![\w-]){_MAINTAINER}(?![\w-])""")


def _tree_files():
    for dirpath, dirnames, filenames in os.walk(_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        rel_dir = os.path.relpath(dirpath, _ROOT)
        if any(part in _SKIP_DIRS for part in rel_dir.split(os.sep)):
            continue
        for name in filenames:
            if name.endswith(_SCAN_EXT):
                yield os.path.join(dirpath, name)


def test_no_maintainer_username_as_an_ssh_user_anywhere():
    """Tree-wide, not a file list. A list is how the first version missed four sites."""
    offenders = []
    for path in _tree_files():
        rel = os.path.relpath(path, _ROOT)
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(lines, 1):
            if not _SSH_USER_LINE.search(line):
                continue
            # This file necessarily names it; and prose explaining the ban is
            # not the ban being broken.
            if rel == os.path.join("agent", "test_ssh_user_defaults.py"):
                continue
            if line.lstrip().startswith(("#", "//")):
                continue
            if _MAINTAINER_LITERAL.search(line):
                offenders.append(f"{rel}:{n}: {line.strip()}")
    assert not offenders, (
        f"the maintainer's username appears as an SSH user — public artifacts must "
        f"not steer an unconfigured install at one person's login:\n"
        + "\n".join(offenders))


def test_the_generic_placeholder_decision_still_holds():
    """The other half: a placeholder is allowed, but it must stay generic.

    Mirrors observability/test_prometheus_containers.py's reasoning so the two
    cannot drift into disagreeing about whether a default is acceptable.
    """
    import sys
    sys.path.insert(0, _ROOT)
    from observability.prometheus_containers import PrometheusContainers
    default = PrometheusContainers("http://prom:9090").ssh_user
    assert default and _MAINTAINER not in default


@pytest.mark.parametrize("relpath,needle", [
    # The paths that spend something on a wrong guess have no default at all.
    ("executor/nodeaction.py", "no SSH user configured"),
    ("worker/entrypoint.py", "no SSH user configured"),
    ("event_runtime/deep_investigation.py", "no SSH user configured"),
])
def test_spending_paths_refuse_by_name(relpath, needle):
    """Each names the setting rather than guessing or failing anonymously."""
    with open(os.path.join(_ROOT, relpath), encoding="utf-8") as fh:
        assert needle in fh.read(), f"{relpath} no longer refuses an unset ssh user"
