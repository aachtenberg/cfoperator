"""Briefing assembly, fed deliberately awkward payloads.

The API shapes this reads are genuinely inconsistent — findings nested under
one endpoint and absent from the other, KB search rows differing per search
mode — so the tests here are mostly "does the briefing survive shape X and
still say the useful thing", not "does it match today's wording".
"""

import pytest

from cfassist.briefing import (
    ATTACH_VERB,
    attach_command,
    build_briefing,
    investigation_facts,
    parse_investigation_ref,
)


# ---- reference parsing ----------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("1889", 1889),
    (1889, 1889),
    ("#1889", 1889),
    ("  1889  ", 1889),
    ("http://cfop.local:8083/investigations/1889", 1889),
    ("https://console/investigations?id=1889", 1889),
])
def test_accepts_what_operators_actually_paste(raw, expected):
    assert parse_investigation_ref(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "   ", "#", "latest", "inv-abc", "0", "-4"])
def test_rejects_anything_it_would_have_to_guess_at(raw):
    """Attaching to the wrong incident is worse than making someone retype."""
    with pytest.raises(ValueError):
        parse_investigation_ref(raw)


def test_attach_command_uses_the_registered_verb():
    assert attach_command(1889) == f"cfassist {ATTACH_VERB} 1889"


# ---- the nested-findings trap ---------------------------------------------


def test_findings_are_read_from_the_nest_not_the_top_level():
    """The trap this whole function exists for.

    /api/investigations/<id> nests provider/response/recommendation inside
    `findings`. A reader that takes the top level gets an empty report and a
    briefing that says nothing while looking fine. Mutation check: make
    investigation_facts read inv.get("response") and this goes red.
    """
    facts = investigation_facts({
        "id": 1889,
        "outcome": "needs_action",
        "findings": {
            "provider": "ollama/gemma4:26b",
            "response": "The pod OOMed twice.",
            "recommendation": "Raise the memory limit.",
        },
    })
    assert facts["report"] == "The pod OOMed twice."
    assert facts["recommendation"] == "Raise the memory limit."
    assert facts["provider"] == "ollama/gemma4:26b"
    assert facts["outcome"] == "needs_action"


def test_list_shaped_row_does_not_crash_the_briefing():
    """A summary row (no `findings` key at all) is a degraded but valid input."""
    text = build_briefing({"investigation": {
        "id": 7, "outcome": "resolved", "trigger": "disk full",
    }})
    assert "investigation #7" in text
    assert "outcome=resolved" in text


def test_string_findings_are_treated_as_the_report():
    facts = investigation_facts({"id": 1, "findings": "raw text report"})
    assert facts["report"] == "raw text report"


def test_empty_report_is_called_out_rather_than_shown_blank():
    """Empty final responses are a known local-model failure mode; a silently
    blank section reads as 'nothing interesting happened'."""
    text = build_briefing({"investigation": {
        "id": 3, "outcome": "monitoring", "findings": {"response": ""},
    }})
    assert "no report recorded" in text


# ---- content --------------------------------------------------------------


def _context(**overrides):
    ctx = {
        "investigation": {
            "id": 1889,
            "outcome": "needs_action",
            "trigger": "KubePodNotReady: immich-kiosk-0",
            "host_id": "headless-gpu",
            "started_at": "2026-08-16T04:11:02Z",
            "completed_at": "2026-08-16T04:12:34Z",
            "duration_seconds": 92.4,
            "tool_calls_count": 9,
            "triage_action": "retry",
            "operator_notes": "restarted by hand, watching",
            "findings": {
                "provider": "ollama/gemma4:26b",
                "response": "Pod restarted 3 times; memory limit is 256Mi.",
                "recommendation": "Raise the memory limit to 512Mi.",
            },
        },
        "remediations": [],
        "learnings": [],
        "learnings_mode": "",
        "warnings": [],
        "console_url": "http://cfop.local:8083",
    }
    ctx.update(overrides)
    return ctx


def test_briefing_carries_the_four_things_the_operator_asks_for():
    """'What is the state, and what did the agent already check?' — answerable
    from the text alone, without a follow-up API call."""
    text = build_briefing(_context())
    assert "investigation #1889" in text          # which incident
    assert "outcome=needs_action" in text          # current state
    assert "KubePodNotReady" in text               # what set it off
    assert "memory limit is 256Mi" in text         # what the agent found
    assert "Raise the memory limit to 512Mi." in text   # what it recommends
    assert "ollama/gemma4:26b" in text             # who concluded it
    assert "9 tool calls" in text


def test_operator_triage_notes_are_included():
    text = build_briefing(_context())
    assert "Operator triage:" in text
    assert "retry — restarted by hand, watching" in text


def test_remediation_rows_render_with_status_and_pr():
    text = build_briefing(_context(remediations=[{
        "id": 42, "status": "needs-human", "remediation_class": "gitops-patch",
        "risk": "medium", "confidence": 0.62,
        "pr_url": "https://github.com/x/y/pull/9",
        "payload": {"title": "Raise immich-kiosk memory limit"},
        "last_error": "diff did not apply",
    }]))
    assert "Linked remediation queue rows (1):" in text
    assert "#42 | needs-human | gitops-patch | risk=medium | confidence=0.62" in text
    assert "https://github.com/x/y/pull/9" in text
    assert "diff did not apply" in text


def test_learnings_from_this_investigation_are_hoisted():
    """FTS-mode rows carry investigation_id; the ones this incident produced
    are the most relevant and go first."""
    text = build_briefing(_context(
        learnings_mode="fts",
        learnings=[
            {"id": 1, "title": "unrelated", "learning_type": "pattern",
             "investigation_id": 5},
            {"id": 2, "title": "from this one", "learning_type": "root_cause",
             "investigation_id": 1889},
        ],
    ))
    assert text.index("from this one") < text.index("unrelated")
    assert "* = from this investigation" in text


def test_hybrid_rows_without_investigation_id_do_not_break_sorting():
    """The hybrid SQL path omits investigation_id entirely. Nothing is hoisted;
    nothing raises. Mutation check: index the key directly instead of .get and
    this goes red."""
    text = build_briefing(_context(
        learnings_mode="hybrid",
        learnings=[
            {"id": 1, "title": "vector hit", "learning_type": "pattern",
             "vector_similarity": 0.81},
        ],
    ))
    assert "vector hit" in text
    assert "search mode: hybrid" in text
    assert "* = from this investigation" not in text


def test_warnings_are_surfaced_so_a_partial_briefing_is_visibly_partial():
    text = build_briefing(_context(warnings=["knowledge search unavailable: boom"]))
    assert "Incomplete briefing:" in text
    assert "knowledge search unavailable: boom" in text


def test_long_reports_are_truncated_with_a_marker():
    text = build_briefing(
        _context(investigation={**_context()["investigation"],
                                "findings": {"response": "x" * 9000}}),
        max_report_chars=500,
    )
    assert "truncated" in text
    assert "x" * 600 not in text


def test_empty_context_still_produces_a_briefing():
    """Defensive: build_briefing is called on whatever the API returned."""
    assert "CFOperator briefing" in build_briefing({})
    assert "CFOperator briefing" in build_briefing(None)
