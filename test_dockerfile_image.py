"""The image must contain everything the deployed processes import.

The Dockerfile copies named paths rather than the whole tree, so adding a
top-level package to the repo does not add it to the image. When that package
is imported at module load — as ``web_server.py`` and ``mcp_server/server.py``
both import ``auth`` — the result is not a missing feature. It is an
ImportError that crash-loops the pod, and :8083 refusing connections.

That is exactly how the auth package shipped: tests all green, image broken.
This walks the code the Dockerfile actually copies, finds every first-party
top-level module it imports, and fails if the image would not contain it.
"""

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).parent
DOCKERFILE = ROOT / "Dockerfile"


def copied_paths() -> set[str]:
    """Sources named by COPY lines, normalised to a repo-relative path."""
    paths = set()
    for line in DOCKERFILE.read_text().splitlines():
        m = re.match(r"^COPY\s+(\S+)\s+(\S+)\s*$", line.strip())
        if m:
            paths.add(m.group(1).rstrip("/"))
    return paths


def copied_modules() -> set[str]:
    """Copied paths expressed as importable names: `auth/` and `web_auth.py`
    both land in /app, so they import as `auth` and `web_auth`."""
    return {p[:-3] if p.endswith(".py") else p for p in copied_paths()}


def is_first_party(name: str) -> bool:
    """True when `name` resolves to a top-level module or package in the repo.

    Bare imports that resolve inside a copied directory (agent/ puts itself on
    PYTHONPATH, so `knowledge_base` means agent/knowledge_base.py) are not
    top-level and are already covered by their directory's own COPY.
    """
    return (ROOT / f"{name}.py").is_file() or (ROOT / name / "__init__.py").is_file()


def top_level_imports(path: pathlib.Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return set()

    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # Relative imports stay within their own package, which the
            # package's own COPY already covers.
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def python_files_in_image() -> list[pathlib.Path]:
    files = []
    for entry in copied_paths():
        target = ROOT / entry
        if target.is_file() and target.suffix == ".py":
            files.append(target)
        elif target.is_dir():
            files.extend(target.rglob("*.py"))
    return files


def test_every_first_party_import_is_copied_into_the_image():
    copied = copied_modules()
    missing = {}

    for path in python_files_in_image():
        rel = path.relative_to(ROOT)
        # A test file is in the image only incidentally; it is not imported by
        # a deployed process, so it must not dictate what the image contains.
        if rel.name.startswith("test_") or "tests" in rel.parts:
            continue
        for name in top_level_imports(path):
            if is_first_party(name) and name not in copied:
                missing.setdefault(name, []).append(str(rel))

    assert not missing, (
        "these first-party modules are imported by code in the image but never "
        "COPYed into it, so the pod will crash-loop on ImportError: "
        + "; ".join(f"{mod} (imported by {', '.join(sorted(users))})"
                    for mod, users in sorted(missing.items()))
    )


def test_the_lockout_runbook_script_is_in_the_image():
    """docs/auth.md tells operators to recover a lost admin with
    `kubectl exec ... python scripts/create_admin.py`. A runbook that names a
    path the image does not have is worse than no runbook, because it is only
    read when someone is already locked out."""
    assert "scripts" in copied_modules()
    assert (ROOT / "scripts" / "create_admin.py").is_file()


def test_the_guard_itself_catches_a_missing_copy():
    """Proves the check above is load-bearing rather than vacuous: drop `auth`
    from the copied set and the same logic must flag it."""
    copied = copied_modules() - {"auth"}
    offenders = {
        name
        for path in (ROOT / "web_server.py", ROOT / "mcp_server" / "server.py")
        for name in top_level_imports(path)
        if is_first_party(name) and name not in copied
    }
    assert "auth" in offenders
