"""The agent's own list of what ``/command`` it will run (CFOP-93).

The console used to keep two hand-written copies of this list and both were
wrong — they disagreed with each other and neither knew about two of the
nine skills. ``list_slash_commands()`` is what the page now renders from,
built from the same two things the chat path dispatches on: ``self.skills``
(``_execute_skill``) and ``_SLASH_SHORTCUTS`` (``_expand_slash_shortcut``).
These pin that nothing else feeds it, and that a skill the agent loads is a
skill the console will show.
"""

from pathlib import Path

import pytest

from agent import CFOperator

REPO = Path(__file__).resolve().parent.parent


def _op(skills):
    op = CFOperator.__new__(CFOperator)
    op.skills = skills
    return op


def _skill(name, description="Does a thing. Use it when things need doing.", args="[target]"):
    return {"name": name, "description": description, "args": args, "instructions": "..."}


# --------------------------------------------------------------------------
# what the list is made of
# --------------------------------------------------------------------------

def test_a_loaded_skill_is_listed_without_anyone_naming_it():
    """The mutation this guards: a hand-maintained list would not know
    ``brand-new-skill`` exists."""
    op = _op({"brand-new-skill": _skill("brand-new-skill", args="<thing>")})
    rows = {r["command"]: r for r in op.list_slash_commands()}
    assert "/brand-new-skill" in rows
    row = rows["/brand-new-skill"]
    assert row == {"command": "/brand-new-skill", "name": "brand-new-skill",
                   "args": "<thing>", "description": "Does a thing.", "kind": "skill"}


def test_every_shortcut_is_listed_from_its_own_row():
    op = _op({})
    rows = {r["command"]: r for r in op.list_slash_commands()}
    for cmd, shortcut in CFOperator._SLASH_SHORTCUTS.items():
        assert cmd in rows, f"{cmd} expands but is not listed"
        assert rows[cmd]["kind"] == "shortcut"
        assert rows[cmd]["args"] == shortcut["args"]
        assert rows[cmd]["description"] == shortcut["description"]
        assert rows[cmd]["name"] == cmd[1:]


def test_skills_come_first_and_sorted_then_shortcuts_in_declared_order():
    op = _op({"zeta": _skill("zeta"), "alpha": _skill("alpha")})
    commands = [r["command"] for r in op.list_slash_commands()]
    assert commands[:2] == ["/alpha", "/zeta"]
    assert commands[2:] == list(CFOperator._SLASH_SHORTCUTS)


def test_nothing_loaded_still_lists_the_shortcuts():
    assert [r["kind"] for r in _op({}).list_slash_commands()] == \
        ["shortcut"] * len(CFOperator._SLASH_SHORTCUTS)
    assert [r["kind"] for r in _op(None).list_slash_commands()] == \
        ["shortcut"] * len(CFOperator._SLASH_SHORTCUTS)


@pytest.mark.parametrize("text, expected", [
    ("Run a systematic investigation of a pod. Use this when a pod is crashing. Keywords: pod.",
     "Run a systematic investigation of a pod."),
    ("Investigate code changes correlated with infrastructure alerts.\nUse when an alert may be caused by a recent deployment.",
     "Investigate code changes correlated with infrastructure alerts."),
    ("No terminal punctuation at all", "No terminal punctuation at all"),
    ("", ""),
    (None, ""),
])
def test_description_is_the_first_sentence(text, expected):
    assert CFOperator._one_line_description(text) == expected


# --------------------------------------------------------------------------
# the shortcut table
# --------------------------------------------------------------------------

def test_every_shortcut_row_is_complete():
    """Each row carries the expansion and what the console shows for it —
    the point of one table is that neither half can go missing."""
    for cmd, row in CFOperator._SLASH_SHORTCUTS.items():
        assert cmd.startswith("/"), cmd
        assert set(row) == {"prompt", "args", "description"}, cmd
        assert row["prompt"] and row["description"], cmd
        # A template with a slot must say what goes in it.
        assert ("{0}" in row["prompt"]) == bool(row["args"]), cmd


@pytest.mark.parametrize("message, expected", [
    ("/stats 6", "Give me the operational summary for the last 6 hours."),
    ("/stats", "Give me the operational summary for the last 24 hours."),
    ("/STATS 2", "Give me the operational summary for the last 2 hours."),
    ("/sweeps", "Show me the recent sweep reports with findings summaries."),
    ("/investigate-pod apps x", "/investigate-pod apps x"),
    ("plain question", "plain question"),
])
def test_shortcut_expansion_still_works_after_the_table_grew(message, expected):
    assert CFOperator._expand_slash_shortcut(_op({}), message) == expected


# --------------------------------------------------------------------------
# what _load_skills reads off the real files
# --------------------------------------------------------------------------

@pytest.fixture
def loaded(monkeypatch):
    monkeypatch.chdir(REPO)  # _load_skills reads Path('skills') relative to cwd
    return CFOperator._load_skills(_op({}))


def test_every_skill_directory_loads(loaded):
    on_disk = {p.parent.name for p in (REPO / "skills").glob("*/SKILL.md")}
    assert on_disk, "skills/ has no SKILL.md files"
    assert set(loaded) == on_disk


def test_argument_hint_is_read_from_frontmatter(loaded):
    assert loaded["investigate-pod"]["args"] == "<namespace> <pod>"
    assert loaded["investigate-host"]["args"] == "<hostname>"
    # An explicit empty hint means "takes nothing" …
    assert loaded["k3s-cluster-health"]["args"] == ""
    # … while no hint at all means the generic free-text target every skill accepts.
    assert loaded["mqtt-top-talkers"]["args"] == "[target]"


def test_the_listing_of_the_real_skills_is_one_line_each(loaded):
    rows = _op(loaded).list_slash_commands()
    skills = [r for r in rows if r["kind"] == "skill"]
    assert len(skills) == len(loaded)
    for row in skills:
        assert row["description"], row["command"]
        assert "\n" not in row["description"], row["command"]
        assert "Keywords:" not in row["description"], row["command"]
