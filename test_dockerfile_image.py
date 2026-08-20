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
import fnmatch
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


# ---------------------------------------------------------------------------
# The mirror of the guard above: everything the image contains must also be
# able to *trigger* a rebuild.
# ---------------------------------------------------------------------------

BUILD_WORKFLOW = ROOT / ".github" / "workflows" / "build-cfoperator-main.yml"


def build_paths_ignore() -> list[str]:
    """The `paths-ignore` globs from the image-build workflow.

    Parsed by hand rather than with a YAML loader so this test has no
    dependency the rest of the root-level suite does not already carry.
    """
    patterns: list[str] = []
    inside = False
    for raw in BUILD_WORKFLOW.read_text().splitlines():
        line = raw.strip()
        if line.startswith("paths-ignore:"):
            inside = True
            continue
        if inside:
            if line.startswith("#"):
                continue
            if line.startswith("- "):
                patterns.append(line[2:].strip().strip("'\""))
                continue
            if line:  # any other key ends the block
                break
    assert patterns, "could not parse paths-ignore — reread the workflow"
    return patterns


def paths_ignore_violations(patterns: list[str], copied: set[str]) -> list[str]:
    """Ignore patterns that would suppress a rebuild for something in the image.

    Extracted from the test so it can be fed a deliberately broken list — a
    check that cannot be run against bad input cannot prove it catches bad
    input.

    Both `foo/**` and `foo/*` are treated as governing the `foo` tree. Only the
    first spelling is in use today, but GitHub Actions honours either, and
    `fnmatch("foo", "foo/*")` is False — so matching on the glob alone would let
    the single-star form skip rebuilds while passing this guard.
    """
    violations = []
    for pattern in patterns:
        root = None
        for suffix in ("/**", "/*"):
            if pattern.endswith(suffix):
                root = pattern[: -len(suffix)]
                break
        if root and root in copied:
            violations.append(f"{pattern!r} ignores COPYed path {root!r}")
            continue
        # Catches file-level entries (a future `config.yaml.example`); `**.md`
        # and friends match nothing in the COPY set and fall through here.
        for path in copied:
            if fnmatch.fnmatch(path, pattern):
                violations.append(f"{pattern!r} matches COPYed path {path!r}")
    return violations


def test_no_ignored_path_is_baked_into_the_image():
    """A path the Dockerfile COPYs must never be in `paths-ignore`.

    Merging to main *is* the deploy here: CI builds the image and commits the
    tag to cfoperator-deploy, which ArgoCD rolls. If a COPYed path is ignored,
    a change to it produces no image, no tag bump and no rollout — the fix
    appears to deploy and does not, then lands later attributed to whatever
    unrelated commit finally triggered a build.

    This has now happened twice (`cfshared/**`, then `observability/**`), both
    caught by review rather than by anything automatic. Hence this test: the
    rule is cheap to state and evidently easy to break.
    """
    violations = paths_ignore_violations(build_paths_ignore(), copied_paths())
    assert not violations, (
        "build-cfoperator-main.yml would skip the image build for a change to "
        "something that ships in the image, so the change would never deploy:\n  "
        + "\n  ".join(violations)
        + "\n\nA path may only be in paths-ignore if it is NOT COPYed by the "
        "Dockerfile. See docs/DEPLOYMENT.md."
    )


COCKPIT_DOCKERFILE = ROOT / "cockpit" / "Dockerfile"


def cockpit_copied_paths() -> set[str]:
    """Repo paths the cockpit image bakes in.

    ``COPY --from=<stage>`` lines take their source from an earlier build stage
    rather than from the build context, so they are not repo inputs and must not
    be treated as ones.
    """
    paths = set()
    for line in COCKPIT_DOCKERFILE.read_text().splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY "):
            continue
        tokens = stripped.split()[1:]
        if any(t.startswith("--from=") for t in tokens):
            continue
        sources = [t for t in tokens if not t.startswith("--")][:-1]
        paths.update(s.rstrip("/") for s in sources)
    return paths


def test_no_ignored_path_is_baked_into_the_cockpit_image():
    """The same rule as above, for the fourth image.

    ``cfassist-go/**`` was correctly in ``paths-ignore`` while the Go tree only
    shipped as a release binary. CFOP-35 builds it into the cockpit image, which
    silently inverts that: an ignored path that is now baked in means a cfassist
    fix never reaches a cockpit pod, and the failure mode is "the fix appears to
    deploy and does not".
    """
    assert cockpit_copied_paths(), "could not parse cockpit/Dockerfile COPY lines"
    violations = paths_ignore_violations(build_paths_ignore(), cockpit_copied_paths())
    assert not violations, (
        "build-cfoperator-main.yml would skip the cockpit image build for a "
        "change to something that ships in it:\n  " + "\n  ".join(violations))


def test_the_cockpit_paths_ignore_guard_catches_the_entry_that_used_to_be_there():
    """Runs the real matcher against the mutated list, so the check above is
    demonstrably not vacuous."""
    copied = cockpit_copied_paths()
    assert any(p.startswith("cfassist-go") for p in copied), (
        "premise moved: the cockpit image no longer bakes in cfassist-go")

    for spelling in ("cfassist-go/**", "cfassist-go/*"):
        violations = paths_ignore_violations(build_paths_ignore() + [spelling], copied)
        assert any("cfassist-go" in v for v in violations), (
            f"the guard missed {spelling!r}, which would silently skip cockpit "
            "rebuilds for the CLI that runs inside it")


def test_the_paths_ignore_guard_catches_a_reintroduced_entry():
    """Re-add the entry that actually shipped broken and confirm it is flagged.

    Runs the real matcher against a mutated list, rather than re-deriving a
    constant and asserting it equals itself.
    """
    copied = copied_paths()
    assert "observability" in copied, "premise moved: observability is not COPYed"

    for spelling in ("observability/**", "observability/*"):
        violations = paths_ignore_violations(build_paths_ignore() + [spelling], copied)
        assert any("observability" in v for v in violations), (
            f"the guard missed {spelling!r}, which Actions honours and which "
            "would silently skip rebuilds for a package that ships in the image"
        )
