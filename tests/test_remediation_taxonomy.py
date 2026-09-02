"""The remediation class taxonomy is duplicated across artifacts — guard it.

Five components carry their own copy of the class list, and they cannot
import each other: the agent (queue + classifier rubric), the worker (a
separate image whose forensics templates ask the LLM to self-label), the
executor (routes on the class), the console (reclassify picker), and the
docs. `agent/knowledge_base.py` already says "keep the class list in sync
with _VALID_REMEDIATION_CLASSES" — a hand-sync instruction with nothing
enforcing it.

CFOP-61 added `k8s-imperative` and had to touch seven files to do it. These
tests fail when the next class is added to some of them and not the rest,
which is the drift this repo has been bitten by before (CFOP-23's docs
honesty pass existed because a second copy of the truth had drifted).

Deliberately NOT a pin of today's five names: each test derives the expected
set from the agent's list, so adding a class correctly keeps them green.
"""

from repo_paths import REPO_ROOT
import re
from pathlib import Path

import sys

ROOT = REPO_ROOT
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT))

from agent.knowledge_base import (  # noqa: E402
    _AUTO_REMEDIATION_CLASSES,
    _REMEDIATION_CLASSES,
)


def _worker_classes():
    src = (ROOT / "worker" / "entrypoint.py").read_text(encoding="utf-8")
    m = re.search(r"_VALID_REMEDIATION_CLASSES = \((.*?)\)", src, re.DOTALL)
    assert m, "worker no longer declares _VALID_REMEDIATION_CLASSES"
    return set(re.findall(r'"([a-z0-9-]+)"', m.group(1)))


def test_worker_and_agent_agree_on_the_class_list():
    """The worker self-labels in a separate image; a class it does not know
    normalizes to 'manual' on arrival, silently losing the label."""
    assert _worker_classes() == set(_REMEDIATION_CLASSES)


def test_every_class_is_defined_in_the_shared_rubric():
    """_REMEDIATION_CLASS_RUBRIC is the single definition both LLM feeds
    read. A queue class absent from it is one the classifier is never told
    about, so it can only ever arrive by normalization accident."""
    import agent.agent as agent_mod
    rubric = agent_mod._REMEDIATION_CLASS_RUBRIC
    for rclass in _REMEDIATION_CLASSES:
        assert f"- {rclass}:" in rubric, f"{rclass} missing from the rubric"


def test_classifier_response_schema_offers_every_class():
    """The JSON schema line in the classifier prompt enumerates the choices;
    a class missing there cannot be returned however good the rubric is."""
    src = (ROOT / "agent" / "agent.py").read_text(encoding="utf-8")
    # Collapse Python's implicit string concatenation first: the schema line
    # is long enough to be wrapped across literals, and the test should not
    # dictate how the source is formatted.
    src = re.sub(r"'\s*\n\s*f?'", "", src)
    # EVERY such schema, not just the first. agent.py carries two — the
    # needs_action classifier and the morning-summary prompt — and checking
    # only the first left the summary copy silently stale when
    # k8s-imperative was added (PR #150 review). Requires the pipe-separated
    # form: a bare "remediation_class": "manual" appears in
    # _CLASSIFIER_SAFE_EXAMPLE and is not a schema.
    schemas = re.findall(r'"remediation_class": "([a-z0-9-]+(?:\|[a-z0-9-]+)+)"', src)
    assert len(schemas) >= 2, (
        f"expected at least 2 class schemas in agent.py, found {len(schemas)} — "
        "if a prompt was removed, update this count deliberately")
    for schema in schemas:
        offered = set(schema.split("|"))
        missing = set(_REMEDIATION_CLASSES) - offered
        assert not missing, f"schema '{schema}' omits {sorted(missing)}"


def test_console_reclassify_offers_every_class():
    """An operator who cannot pick a class in the console cannot correct a
    misclassified row into it — which is how #49 was reclassified at all."""
    html = (ROOT / "ui" / "remediations.html").read_text(encoding="utf-8")
    m = re.search(r"pickRow\('class',\[(.*?)\]", html)
    assert m, "reclassify picker not found"
    offered = set(re.findall(r"'([a-z0-9-]+)'", m.group(1)))
    assert set(_REMEDIATION_CLASSES) <= offered


def test_forensics_templates_offer_every_class():
    for name in ("host-forensics.md", "boot-forensics.md"):
        text = (ROOT / "worker" / "templates" / name).read_text(encoding="utf-8")
        m = re.search(r"REMEDIATION_CLASS: <one of: (.*?)>", text)
        assert m, f"{name} no longer states the class list"
        offered = {c.strip() for c in m.group(1).split("|")}
        assert _worker_classes() <= offered, name


# ---- the CFOP-61 invariant ------------------------------------------------


def test_auto_eligible_classes_all_have_an_executor_path():
    """The bug this issue exists for: k8s-action was auto-eligible while the
    executor could only run node-action (SSH) or a GitOps diff. Anything the
    queue will drain unattended must have somewhere to land."""
    src = (ROOT / "executor" / "entrypoint.py").read_text(encoding="utf-8")
    m = re.search(r"_NO_EXECUTOR_PATH = \((.*?)\)", src, re.DOTALL)
    assert m, "executor no longer declares _NO_EXECUTOR_PATH"
    no_path = set(re.findall(r'"([a-z0-9-]+)"', m.group(1)))
    overlap = set(_AUTO_REMEDIATION_CLASSES) & no_path
    assert not overlap, (
        f"{overlap} would auto-drain into a class the executor cannot run — "
        "give it a runner or take it out of _AUTO_REMEDIATION_CLASSES")


def test_k8s_imperative_is_not_auto_eligible():
    """Until something can run a one-off cluster verb, it parks for a human."""
    assert "k8s-imperative" in _REMEDIATION_CLASSES
    assert "k8s-imperative" not in _AUTO_REMEDIATION_CLASSES


def test_data_fix_and_external_system_are_not_auto_eligible():
    """CFOP-80: new classes exist to park honestly. Auto-draining one would
    spend an executor Job to say 'nothing can apply this class'."""
    for rclass in ("data-fix", "external-system"):
        assert rclass in _REMEDIATION_CLASSES
        assert rclass not in _AUTO_REMEDIATION_CLASSES


def test_the_table_check_admits_every_class():
    """The CHECK is rendered from _REMEDIATION_CLASSES, not written out again.

    PR #150 review: the model's CheckConstraint hard-coded four classes while
    the tuple grew to five, so normalize_remediation_fields would pass
    'k8s-imperative' and the INSERT would then die — the class that exists to
    PARK a row failing to record one, with _maybe_queue_remediation swallowing
    the error into None.
    """
    from agent.knowledge_base import (
        REMEDIATION_CLASS_CHECK_SQL,
        constraint_admits_outcomes,
    )
    assert constraint_admits_outcomes(
        REMEDIATION_CLASS_CHECK_SQL, set(_REMEDIATION_CLASSES))


def test_a_startup_widener_exists_for_the_class_check():
    """create_all never alters an existing table, so the model alone does not
    reach a database that already exists — the CFOP-20 lesson, on a second
    table. Without this, main gets the new class and production rejects it."""
    from agent.knowledge_base import KnowledgeBase
    assert hasattr(KnowledgeBase, "_ensure_remediation_class_constraint")
    src = (ROOT / "agent" / "knowledge_base.py").read_text(encoding="utf-8")
    init = src[src.index("def initialize_schema"):src.index("def _ensure_outcome_constraint")]
    assert "_ensure_remediation_class_constraint" in init, (
        "the widener exists but schema init never calls it")
