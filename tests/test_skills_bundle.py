"""The skills baked into cfassist must be the skills in this repo.

``cfassist-go`` is its own Go module, and ``go:embed`` cannot reach outside a
module's directory — so the nine ``skills/*/SKILL.md`` files are *copied* into
``cfassist-go/internal/skills/bundled/`` and committed. A copy nobody checks
goes stale silently, and the failure is the worst kind: an operator runs
``/skill investigate-pod`` mid-incident and gets last month's playbook, with
nothing anywhere saying so.

Same arrangement as ``DEFAULT_CFASSIST_VERSION`` and the attach verb: two
artifacts that must agree, kept honest by a test rather than by an import that
the language will not allow.

Run ``make sync-skills`` in ``cfassist-go/`` to fix a failure here.
"""

from repo_paths import REPO_ROOT
import pathlib

ROOT = REPO_ROOT
SKILLS = ROOT / "skills"
BUNDLED = ROOT / "cfassist-go" / "internal" / "skills" / "bundled"


def skill_files(root: pathlib.Path) -> dict[str, str]:
    return {p.parent.name: p.read_text(encoding="utf-8") for p in root.glob("*/SKILL.md")}


def test_the_bundled_copy_is_the_repo_copy():
    source = skill_files(SKILLS)
    bundled = skill_files(BUNDLED)

    assert source, "skills/ has no SKILL.md files"

    missing = set(source) - set(bundled)
    assert not missing, (
        f"skills not baked into cfassist: {sorted(missing)}. "
        "Run `cd cfassist-go && make sync-skills`."
    )

    extra = set(bundled) - set(source)
    assert not extra, (
        f"cfassist bundles skills that no longer exist in skills/: {sorted(extra)}. "
        "Run `cd cfassist-go && make sync-skills`."
    )

    drifted = sorted(name for name in source if source[name] != bundled[name])
    assert not drifted, (
        f"bundled copies are stale: {drifted}. An operator would get the old "
        "playbook mid-incident. Run `cd cfassist-go && make sync-skills`."
    )


def test_the_sync_target_exists_and_names_both_trees():
    """Without it the fix for a failure above is folklore."""
    makefile = (ROOT / "cfassist-go" / "Makefile").read_text()
    assert "sync-skills" in makefile, "cfassist-go/Makefile must offer `make sync-skills`"
    assert "internal/skills/bundled" in makefile


def test_every_bundled_skill_has_frontmatter_cfassist_can_parse():
    """cfassist skips a skill whose frontmatter will not parse — silently, by
    design, so one bad file cannot cost an operator the other eight. That makes
    a malformed file invisible, so it is checked here instead."""
    import yaml

    for name, text in skill_files(BUNDLED).items():
        assert text.startswith("---"), f"{name}: no frontmatter, cfassist would skip it"
        _, frontmatter, body = text.split("---", 2)
        meta = yaml.safe_load(frontmatter)
        assert isinstance(meta, dict), f"{name}: frontmatter is not a mapping"
        assert meta.get("description"), f"{name}: no description — it would list as a bare name"
        assert body.strip(), f"{name}: empty body"


# --- the shared target format ------------------------------------------------

MCP_PLAYBOOKS = ROOT / "mcp_server" / "prompts" / "playbooks.py"
GO_SKILLS = ROOT / "cfassist-go" / "internal" / "skills" / "skills.go"

# Both sides append this to the body when a playbook is aimed at something. The
# literal is written with escaped newlines because that is how it appears in
# both sources — an f-string in Python, a quoted string in Go.
TARGET_SUFFIX = r"## Target\n\nApply this playbook to: "


def test_cfassist_and_mcp_hosts_render_the_same_targeted_playbook():
    """"Run the pod playbook against immich-kiosk-0" must mean one thing.

    Two implementations, two languages, two deploy artifacts: cfassist renders
    a skill for its own session, ``playbooks.py`` renders the same file for MCP
    hosts. Nothing in either language makes them agree, and the Go test asserts
    its own literal — so a wording change in ``playbooks.py`` would leave the
    two producing different prompts with CI green. This is the assert that
    notices.
    """
    for path in (MCP_PLAYBOOKS, GO_SKILLS):
        assert TARGET_SUFFIX in path.read_text(encoding="utf-8"), (
            f"{path.relative_to(ROOT)} no longer renders a targeted playbook as "
            f'"{TARGET_SUFFIX}". cfassist and MCP hosts must produce identical text '
            "for the same skill and target — change both, or neither."
        )
