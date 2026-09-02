"""Cockpit session write-back — the server half (CFOP-37).

What a human and an agent work out in a cockpit has to survive the cockpit.
These guard the three things that make that true rather than merely attempted:

* the session appends *beside* the investigation and never edits ``findings``,
  which is the corpus later triage decisions reason from;
* a session written back is a session that can be *read* back — by the console,
  by the next attach, by anything that asks for the investigation;
* the write is authorised by the session's own dying credential (``investigate``
  scope), not by an admin role the session does not have.
"""

from repo_paths import REPO_ROOT
import json
import os
import pathlib
import threading

import pytest
from flask import Flask
from sqlalchemy import create_engine

from auth.models import EVENT_COCKPIT_SESSION, ROLE_ADMIN, ROLE_MEMBER
from auth.store import AuthStore

PASSWORD = "correct-horse-battery-staple"


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

class FakeKB:
    """Enough knowledge base to exercise the endpoint, and no more.

    Records what it was asked to store so the tests can assert the *shape* of
    the write — which is the part that has to keep working when the real KB is
    a Postgres nobody wants in a unit test.
    """

    #: Sentinel so `FakeKB(investigation=None)` can mean "there is no such
    #: investigation" rather than "use the default one" — which is the whole
    #: point of the 404 test.
    _DEFAULT = object()

    def __init__(self, investigation=_DEFAULT):
        self.investigation = (
            {'id': 1889, 'trigger': 'mount hung', 'findings': {'response': 'agent said this'}}
            if investigation is FakeKB._DEFAULT else investigation)
        self.sessions = []
        self.raised = None

    def get_investigation(self, investigation_id):
        if self.raised:
            raise self.raised
        return dict(self.investigation) if self.investigation else None

    def record_cockpit_session(self, investigation_id, summary, outcome, actor,
                               detail=None, degraded=False):
        self.sessions.append({
            'investigation_id': investigation_id, 'summary': summary,
            'outcome': outcome, 'actor': actor, 'detail': detail or {},
            'degraded': degraded,
        })
        return len(self.sessions)


def _client(*, kb=None, store=None, auth_disabled=True):
    from unittest.mock import MagicMock

    from web_auth import install_auth
    from web_server import WebServer

    operator = MagicMock()
    operator.kb = kb if kb is not None else FakeKB()
    operator.config = {}

    server = WebServer.__new__(WebServer)
    server.operator = operator
    server.host, server.port = "localhost", 0
    server.app = Flask(__name__)
    server._chat_sessions = {}
    server._sessions_lock = threading.Lock()
    server.auth_store = store
    server._setup_routes()

    prior = os.environ.get("CFOP_AUTH_DISABLED")
    os.environ["CFOP_AUTH_DISABLED"] = "1" if auth_disabled else ""
    try:
        install_auth(server.app, store=store)
    finally:
        if prior is None:
            os.environ.pop("CFOP_AUTH_DISABLED", None)
        else:
            os.environ["CFOP_AUTH_DISABLED"] = prior
    return server.app.test_client(), server


def _post(client, investigation_id=1889, **body):
    payload = {'summary': 'the mount was stale; a remount cleared it',
               'outcome': 'resolved'}
    payload.update(body)
    return client.post(f'/api/investigations/{investigation_id}/session', json=payload)


# --------------------------------------------------------------------------
# the write
# --------------------------------------------------------------------------

def test_a_session_is_appended_with_its_summary_and_outcome():
    kb = FakeKB()
    client, _server = _client(kb=kb)
    resp = _post(client, tier='host', host='raspberrypi5',
                 duration_seconds=640, exchanges=12, learning_id=77,
                 commands=['systemctl restart mnt-nas.mount'])
    assert resp.status_code == 201
    assert len(kb.sessions) == 1
    rec = kb.sessions[0]
    assert rec['outcome'] == 'resolved'
    assert 'remount' in rec['summary']
    assert rec['detail']['tier'] == 'host'
    assert rec['detail']['host'] == 'raspberrypi5'
    assert rec['detail']['duration_seconds'] == 640
    assert rec['detail']['learning_id'] == 77
    assert rec['detail']['commands'] == ['systemctl restart mnt-nas.mount']


def test_the_investigations_findings_are_never_touched():
    """MUTATION GUARD, and the reason this feature stores an event at all.

    ``findings`` is what find_similar_investigations_hybrid cites as precedent
    to later triage decisions; the triage endpoint already records that the
    agent's finding stays intact while a human's verdict lives elsewhere. A
    session summary is a human verdict by another name. Route the write-back
    into findings and this fails."""
    kb = FakeKB()
    client, _server = _client(kb=kb)
    _post(client)
    assert kb.investigation['findings'] == {'response': 'agent said this'}, (
        "the write-back edited the corpus that future triage reasons from")


def test_a_session_with_no_summary_is_refused():
    """A row saying a human was here and nothing about what they found is worse
    than no row: it looks like write-back working."""
    kb = FakeKB()
    client, _server = _client(kb=kb)
    assert _post(client, summary='').status_code == 400
    assert _post(client, summary='   ').status_code == 400
    assert kb.sessions == []


def test_an_outcome_outside_the_vocabulary_is_refused():
    """One client inventing a word is how a vocabulary drifts into uselessness."""
    kb = FakeKB()
    client, _server = _client(kb=kb)
    resp = _post(client, outcome='fixed-it-good')
    assert resp.status_code == 400
    assert 'resolved' in resp.get_json()['error']
    assert kb.sessions == []


@pytest.mark.parametrize("outcome", [
    'resolved', 'mitigated', 'diagnosed', 'no_change', 'inconclusive', 'escalated'])
def test_every_documented_outcome_is_accepted(outcome):
    client, _server = _client()
    assert _post(client, outcome=outcome).status_code == 201


def test_an_unknown_investigation_is_a_404_not_an_orphan_row():
    kb = FakeKB(investigation=None)
    client, _server = _client(kb=kb)
    assert _post(client, investigation_id=4242).status_code == 404
    assert kb.sessions == []


def test_a_degraded_summary_is_stored_and_marked():
    """The issue's own instruction is to store the raw tail rather than
    nothing. Storing it unmarked would be worse than either — a transcript
    fragment read as a conclusion."""
    kb = FakeKB()
    client, _server = _client(kb=kb)
    resp = _post(client, degraded=True, outcome='inconclusive',
                 summary='(session summary unavailable — raw transcript tail)\n\nuser: ...')
    assert resp.status_code == 201
    assert resp.get_json()['degraded'] is True
    assert kb.sessions[0]['degraded'] is True


def test_junk_counters_do_not_cost_the_record():
    """The summary is the part that matters; a nonsense duration is not a
    reason to throw a session away."""
    kb = FakeKB()
    client, _server = _client(kb=kb)
    assert _post(client, duration_seconds='ages', exchanges=-4).status_code == 201
    assert kb.sessions[0]['detail']['duration_seconds'] == 0
    assert kb.sessions[0]['detail']['exchanges'] == 0


def test_oversized_fields_are_bounded():
    kb = FakeKB()
    client, _server = _client(kb=kb)
    _post(client, summary='x' * 50000, commands=['y' * 900] * 50)
    rec = kb.sessions[0]
    assert len(rec['summary']) <= 20000
    assert len(rec['detail']['commands']) <= 20
    assert all(len(c) <= 300 for c in rec['detail']['commands'])


# --------------------------------------------------------------------------
# who may write
# --------------------------------------------------------------------------

@pytest.fixture
def store():
    s = AuthStore(engine=create_engine("sqlite://"))
    s.ensure_schema()
    return s


def _token(store, scopes, role=ROLE_ADMIN):
    user = store.create_user("u-" + scopes[0], PASSWORD, role=role)
    _row, secret = store.create_token("t-" + scopes[0], scopes,
                                      created_by=user["id"], creator_role=role)
    return secret


def test_the_sessions_own_token_can_record_it(store):
    """The whole point: the credential minted for this investigation, which
    dies with the session, is the one that writes what the session learned.
    Gating on an admin role would mean it could not."""
    kb = FakeKB()
    client, _server = _client(kb=kb, store=store, auth_disabled=False)
    secret = _token(store, ["investigate"])
    resp = client.post('/api/investigations/1889/session',
                       json={'summary': 's', 'outcome': 'resolved'},
                       headers={'Authorization': f'Bearer {secret}'})
    assert resp.status_code == 201
    assert len(kb.sessions) == 1


def test_a_read_only_token_cannot_record_a_session(store):
    kb = FakeKB()
    client, _server = _client(kb=kb, store=store, auth_disabled=False)
    secret = _token(store, ["read"], role=ROLE_MEMBER)
    resp = client.post('/api/investigations/1889/session',
                       json={'summary': 's', 'outcome': 'resolved'},
                       headers={'Authorization': f'Bearer {secret}'})
    assert resp.status_code == 403
    assert kb.sessions == []


def test_an_anonymous_caller_cannot_record_a_session(store):
    kb = FakeKB()
    client, _server = _client(kb=kb, store=store, auth_disabled=False)
    resp = client.post('/api/investigations/1889/session',
                       json={'summary': 's', 'outcome': 'resolved'})
    assert resp.status_code == 401
    assert kb.sessions == []


# --------------------------------------------------------------------------
# the audit half
# --------------------------------------------------------------------------

def test_the_session_lands_in_the_audit_log_with_who_where_and_what(store):
    """The issue asked for this in changerecord. That service's Intent is an
    approval workflow for a *proposed* change — remediation_id, commands,
    executor image — and a session is none of those: it already happened and
    needed no approval. The audit log already carries actor, investigation and
    scopes for the same session's token mint (CFOP-32) and tier/host (CFOP-36);
    this closes the row with duration and outcome."""
    kb = FakeKB()
    client, _server = _client(kb=kb, store=store, auth_disabled=False)
    secret = _token(store, ["investigate"])
    client.post('/api/investigations/1889/session',
                json={'summary': 's', 'outcome': 'mitigated', 'tier': 'container',
                      'host': 'ubuntu-llm-01', 'duration_seconds': 300, 'learning_id': 9},
                headers={'Authorization': f'Bearer {secret}'})

    rows = [e for e in store.recent_audit(limit=50) if e['event'] == EVENT_COCKPIT_SESSION]
    assert len(rows) == 1, "the session produced no audit row"
    detail = rows[0]['detail']
    assert detail['outcome'] == 'mitigated'
    assert detail['tier'] == 'container'
    assert detail['host'] == 'ubuntu-llm-01'
    assert detail['duration_seconds'] == 300
    assert detail['learning_id'] == 9
    assert detail['investigation_id'] == 1889
    assert rows[0]['actor'], "an audit row with no actor answers none of the question"
    assert rows[0]['target'] == 'investigation:1889'


def test_a_missing_audit_store_does_not_cost_the_session_record():
    """Legacy env-credential installs have no auth store. Losing the session
    because it could not be audited would be the wrong trade — the record is
    the product, the audit row is corroboration."""
    kb = FakeKB()
    client, _server = _client(kb=kb, store=None)
    assert _post(client).status_code == 201
    assert len(kb.sessions) == 1


# --------------------------------------------------------------------------
# the row shape the console and the briefing both read
# --------------------------------------------------------------------------

class _FakeRow:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeQuery:
    """Just enough SQLAlchemy chaining to hand back fixed rows."""

    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **kw):
        return self

    def order_by(self, *a, **kw):
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *a, **kw):
        return _FakeQuery(self._rows)


def _shape(rows):
    """The real row-shaping, without a Postgres. Imported here rather than at
    module scope because `agent.knowledge_base` only resolves with agent/ on
    the path, which is how the suite runs it."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(REPO_ROOT / "agent"))
    from knowledge_base import KnowledgeBase
    return KnowledgeBase._cockpit_sessions(_FakeSession(rows), 1889, 10)


def test_the_session_row_carries_what_both_readers_render():
    """The console drawer and the attach briefing both render this dict. A
    field dropped here is a session that was recorded and cannot be seen —
    which is indistinguishable, to an operator, from write-back not working."""
    from datetime import datetime, timezone

    out = _shape([_FakeRow(
        event_at=datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc),
        action_type="resolved", action_target="admin",
        reasoning_text="the mount was stale",
        tool_output={"tier": "host", "host": "raspberrypi5",
                     "duration_seconds": 640, "commands": ["findmnt /mnt/nas"]},
        success=True)])
    assert len(out) == 1
    row = out[0]
    assert row["outcome"] == "resolved"
    assert row["actor"] == "admin"
    assert row["summary"] == "the mount was stale"
    assert row["detail"]["tier"] == "host"
    assert row["detail"]["commands"] == ["findmnt /mnt/nas"]
    assert row["degraded"] is False
    assert row["recorded_at"].startswith("2026-08-21")


def test_a_degraded_row_reads_as_degraded():
    """success=False is how a raw tail is stored; `degraded` is how both
    readers know not to present it as a conclusion."""
    out = _shape([_FakeRow(event_at=None, action_type="inconclusive",
                           action_target="admin", reasoning_text="user: ...",
                           tool_output=None, success=False)])
    assert out[0]["degraded"] is True
    assert out[0]["detail"] == {}, "a null detail must not become a null deref downstream"


# --------------------------------------------------------------------------
# the constraint that would have made all of the above a 500 (CFOP-20's lesson)
# --------------------------------------------------------------------------

def _kb_module():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(REPO_ROOT / "agent"))
    import knowledge_base
    return knowledge_base


def test_the_event_type_check_admits_a_cockpit_session():
    """REGRESSION GUARD, and the one that matters most in this file.

    ``investigation_events`` carries a CHECK on ``event_type``. It was written
    with four values, and ``create_all`` never alters an existing table — so a
    write-back storing ``cockpit_session`` would have failed the INSERT on
    every database in existence, 500'd the endpoint, and shown the operator
    "the session was NOT recorded" at the end of every session.

    Nothing else here could catch it: the endpoint tests fake the knowledge
    base, so the suite never inserts into this table. This asserts the
    *rendered constraint*, which is the thing the database actually enforces.
    """
    kb = _kb_module()
    assert kb.COCKPIT_SESSION_EVENT in kb.VALID_EVENT_TYPES
    assert kb.constraint_admits_outcomes(
        kb.EVENT_TYPE_CHECK_SQL, {kb.COCKPIT_SESSION_EVENT}), (
        f"the CHECK would reject a cockpit session: {kb.EVENT_TYPE_CHECK_SQL}")


def test_the_event_type_check_is_generated_not_hand_written():
    """Two hand-maintained lists is exactly how 'needs_action' became
    unwritable on investigations (CFOP-20) and 'k8s-imperative' on the
    remediation queue (PR #150). Third table, same rule."""
    kb = _kb_module()
    assert kb.constraint_admits_outcomes(kb.EVENT_TYPE_CHECK_SQL, set(kb.VALID_EVENT_TYPES))
    source = (REPO_ROOT / "agent" / "knowledge_base.py").read_text()
    assert "CheckConstraint(EVENT_TYPE_CHECK_SQL, name='valid_event_type')" in source, (
        "the event-type CHECK is spelled out again instead of generated from "
        "VALID_EVENT_TYPES")


def test_an_existing_database_gets_the_constraint_widened_at_boot():
    """``create_all`` only creates. Every prod database predates this event
    type, so the widener has to run — and has to run in the process that then
    writes, before the first write that needs it."""
    source = (REPO_ROOT / "agent" / "knowledge_base.py").read_text()
    assert "_ensure_event_type_constraint" in source
    init = source[source.index("def initialize_schema"):]
    init = init[:init.index("\n    def ")]
    assert "_ensure_event_type_constraint()" in init, (
        "the widener exists but nothing calls it at schema init")
