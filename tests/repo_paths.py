"""The repository root, resolved once, for the suites under ``tests/``.

Every root-level suite used to sit beside the code and spell this as
``Path(__file__).parent`` — correct only while the file lived in the repo
root. When the suites moved into ``tests/`` (CFOP-155) that expression
silently became the ``tests/`` directory, and *silently* is the operative
word: a large share of these suites work by scanning a directory —
``ui/*.html``, ``charts/cfoperator/templates/*.yaml``,
``.github/workflows/*.yml``, ``skills/*/SKILL.md``. Point one of those at the
wrong directory and the glob returns nothing, the ``for`` body never runs and
the assertion never fires. The suite goes green having checked nothing, which
is worse than the red it replaced.

So the root is computed in exactly one place, and that place refuses to hand
back a directory that is not the repository. The sentinel check below is the
whole point of this module; the convenience is incidental.

Import it as a bare module — ``tests/`` lands on ``sys.path`` because these
files have no package ``__init__.py``, the same mechanism that lets
``test_console_common`` import ``test_console_nav``:

    from repo_paths import REPO_ROOT
"""

from pathlib import Path

# tests/repo_paths.py -> tests/ -> repo root
REPO_ROOT = Path(__file__).resolve().parent.parent

# Files that exist in this repository and nowhere else, spanning the trees the
# scanning suites actually read. If the path arithmetic above ever drifts —
# another directory level, a vendored copy, a sdist that ships tests/ without
# its parent — this fails loudly at import time instead of handing every
# caller a directory whose globs are quietly empty.
_SENTINELS = (
    "Dockerfile",
    "requirements.txt",
    "ui",
    "agent",
    ".github/workflows",
)

_missing = [rel for rel in _SENTINELS if not (REPO_ROOT / rel).exists()]
if _missing:  # pragma: no cover - only reachable when the layout is broken
    raise RuntimeError(
        f"repo_paths.REPO_ROOT resolved to {REPO_ROOT}, which is missing "
        f"{', '.join(_missing)}. The suites under tests/ scan the repository "
        "by globbing these trees; a wrong root makes them pass vacuously "
        "rather than fail. Fix the path arithmetic in tests/repo_paths.py."
    )

__all__ = ["REPO_ROOT"]
