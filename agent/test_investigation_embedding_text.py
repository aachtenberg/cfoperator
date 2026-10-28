#!/usr/bin/env python3
"""The text an investigation is embedded as, built from the findings the agent
actually writes (CFOP-542).

For its whole life `create_investigation_text` read `summary`, `hypothesis`,
`evidence`, `learnings` and `actions` out of findings, and no writer in
agent.py has ever produced those keys -- every path stores `response` and
`recommendation`. Measured on live rows on 2026-10-28, every investigation
embedding was `Issue: <trigger>\\nOutcome: <outcome>`: the diagnosis was stored
in the row and absent from its vector and its tsvector. These feed the builder
the live shape and pin what must and must not be in the text.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from embedding_service import EmbeddingService  # noqa: E402


def _builder() -> EmbeddingService:
    # No endpoint, no cache: create_investigation_text touches neither.
    return EmbeddingService.__new__(EmbeddingService)


#: Findings in the shape agent.py stores them (the main tool-loop path, with
#: every optional key present), taken from investigation #4946 on 2026-10-28.
LIVE_FINDINGS = {
    'response': ("The plane-pgdb-wl-0 PostgreSQL database is logging foreign key "
                 "constraint violations. An UPDATE or DELETE on the issues table is "
                 "rejected because issue_activities rows still reference it."),
    'recommendation': ("Update the schema to include ON DELETE CASCADE for the "
                       "issue_activities foreign key, or delete dependents first."),
    'provider': 'ollama/gemma4:26b',
    'tool_calls': 7,
    'stop_reason': 'stagnation',
    'remediation_id': 401,
    'fix': {'observed': [{'source': 'alert_details', 'value': 'violates foreign key'}]},
    'similar_past': [{'id': 4945, 'outcome': 'needs_action', 'similarity': 0.635,
                      'trigger': 'plane-pgdb logging a foreign-key constraint violation'}],
    'opened_prs': ['https://github.com/example/repo/pull/1'],
}

LIVE_INVESTIGATION = {
    'trigger': ("Plane Postgres (plane-pgdb-wl-0) logged a data-integrity error: an "
                "update/delete on the 'issues' table was rejected by a foreign-key "
                "constraint from 'issue_activities'."),
    'findings': LIVE_FINDINGS,
    'outcome': 'needs_action',
}


def test_the_diagnosis_is_in_the_text():
    text = _builder().create_investigation_text(LIVE_INVESTIGATION)
    assert LIVE_FINDINGS['response'] in text
    assert LIVE_FINDINGS['recommendation'] in text
    assert LIVE_INVESTIGATION['trigger'] in text
    assert "Outcome: needs_action" in text


def test_the_text_is_more_than_the_trigger_and_outcome():
    """The regression itself: the live shape used to produce two lines."""
    text = _builder().create_investigation_text(LIVE_INVESTIGATION)
    assert len(text.splitlines()) > 2
    assert len(text) > len(LIVE_INVESTIGATION['trigger']) + len("Issue: \nOutcome: needs_action")


def test_outcome_precedes_the_response():
    """The bound head-truncates (CFOP-84) and the response is up to 5000
    chars, so the outcome must sit before it or it is the first thing cut."""
    text = _builder().create_investigation_text(LIVE_INVESTIGATION)
    assert text.index("Outcome:") < text.index("Summary:") < text.index("Findings:")


def test_the_response_is_the_last_block():
    text = _builder().create_investigation_text(LIVE_INVESTIGATION)
    assert text.splitlines()[-1].startswith(EmbeddingService.FINDINGS_MARKER + " ")


def test_bookkeeping_and_citations_are_not_embedded():
    """What is excluded is a decision, not an accident: every key listed in
    FINDINGS_KEYS_NOT_EMBEDDED is present in the live shape above, and none
    of its values may reach the text. similar_past in particular would make a
    row similar to what it cited instead of to its own diagnosis."""
    text = _builder().create_investigation_text(LIVE_INVESTIGATION)
    present = set(LIVE_FINDINGS) & EmbeddingService.FINDINGS_KEYS_NOT_EMBEDDED
    assert present, "the live fixture should carry excluded keys for this to test anything"
    assert 'ollama/gemma4:26b' not in text
    assert 'stagnation' not in text
    assert '4945' not in text and '0.635' not in text
    assert 'alert_details' not in text
    assert 'pull/1' not in text


def test_a_preflight_skip_still_embeds_its_one_line_verdict():
    """The preflight path stores the same string as response and
    recommendation; it is short, but it is the record's only content."""
    rec = "No action needed — pod healthy now (last restart 2h, recovered)."
    text = _builder().create_investigation_text({
        'trigger': 'immich-kiosk was OOMKilled and restarted',
        'findings': {'response': rec, 'tool_calls': 0, 'recommendation': rec,
                     'preflight_skip': True},
        'outcome': 'monitoring',
    })
    assert rec in text
    assert "preflight" not in text.lower()


def test_an_empty_response_adds_no_findings_line():
    """No Findings: line with nothing to say: get_unindexed_investigations
    treats a stored text without that line as pre-fix only when the row has
    a response, so an empty response must not be re-selected forever."""
    text = _builder().create_investigation_text({
        'trigger': 't', 'findings': {'response': '   ', 'recommendation': ''},
        'outcome': 'failed'})
    assert EmbeddingService.FINDINGS_MARKER not in text
    assert text == "Issue: t\nOutcome: failed"


def test_the_legacy_keys_are_still_honoured():
    """A dict that does carry summary/evidence/actions (nothing in agent.py
    writes one, but the shape was the builder's contract) keeps working, and
    its summary wins over a recommendation."""
    text = _builder().create_investigation_text({
        'trigger': 'disk full on /var',
        'findings': {'summary': 'log rotation stopped', 'recommendation': 'ignored',
                     'evidence': ['journal 9G', 'logrotate.timer inactive'],
                     'actions_taken': ['started logrotate.timer']},
        'outcome': 'resolved',
    })
    assert "Summary: log rotation stopped" in text
    assert "ignored" not in text
    assert "Evidence: journal 9G; logrotate.timer inactive" in text
    assert "Actions: started logrotate.timer" in text


def test_a_string_findings_is_the_findings_block():
    text = _builder().create_investigation_text(
        {'trigger': 't', 'findings': 'plain text', 'outcome': 'monitoring'})
    assert text.endswith("Findings: plain text")
