"""The investigations list says what became of each row's remediation (CFOP-709).

The Inbox and the rail's count ask one question of an investigation: is it
still waiting on a human? Part of the answer is whether its recommendation was
already decided, and the list row is where that has to be, because the rail
asks on every page. These run the real queries against a real (sqlite) table:
the linking rule is the thing under test, and a mocked session would only
replay the calls back.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import knowledge_base as kb


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    kb.RemediationQueue.__table__.create(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


_T0 = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def _row(session, rid, investigation_id, status, minutes=0):
    session.add(kb.RemediationQueue(
        id=rid, investigation_id=investigation_id, host_id="h", status=status,
        remediation_class="manual", created_at=_T0 + timedelta(minutes=minutes),
        payload={}))
    session.commit()


def _inv(iid, remediation_id=None):
    findings = {} if remediation_id is None else {"remediation_id": remediation_id}
    return SimpleNamespace(id=iid, findings=findings)


def test_an_investigation_with_no_row_is_absent(session):
    _row(session, 1, 10, "resolved")
    assert kb._linked_remediation_statuses(session, [_inv(11)]) == {}


def test_no_investigations_asks_nothing(session):
    assert kb._linked_remediation_statuses(session, []) == {}


def test_the_row_fed_by_the_investigation_is_its_status(session):
    _row(session, 1, 10, "rejected")
    assert kb._linked_remediation_statuses(session, [_inv(10)]) == {10: "rejected"}


def test_the_newest_row_wins_when_an_investigation_fed_several(session):
    """The pages read /api/remediations newest first and keep the first row
    they see per investigation. A re-filed recommendation must not be reported
    by the status of the row it replaced."""
    _row(session, 1, 10, "rejected", minutes=0)
    _row(session, 2, 10, "needs-human", minutes=5)
    assert kb._linked_remediation_statuses(session, [_inv(10)]) == {10: "needs-human"}


def test_a_deduped_repeat_is_linked_through_its_findings(session):
    """A repeat that folded onto an existing row has no row of its own: the
    row is keyed under the FIRST investigation, and only the id on the
    repeat's findings points at it."""
    _row(session, 7, 10, "resolved")
    out = kb._linked_remediation_statuses(session, [_inv(10), _inv(11, remediation_id=7)])
    assert out == {10: "resolved", 11: "resolved"}


def test_an_own_row_wins_over_the_id_on_the_findings(session):
    _row(session, 7, 10, "resolved", minutes=0)
    _row(session, 8, 11, "queued", minutes=1)
    out = kb._linked_remediation_statuses(session, [_inv(11, remediation_id=7)])
    assert out == {11: "queued"}


def test_a_findings_id_that_names_no_row_is_absent(session):
    assert kb._linked_remediation_statuses(session, [_inv(11, remediation_id=999)]) == {}


def test_the_list_payload_carries_the_status():
    """The field has to reach the list row, not only exist as a helper: the
    rail's count is computed from /api/investigations and nothing else."""
    from pathlib import Path
    src = Path(kb.__file__).read_text(encoding="utf-8")
    start = src.index("def get_recent_investigations")
    body = src[start:start + 3000]
    assert "_linked_remediation_statuses(session, investigations)" in body
    assert '"remediation_status": remediation_status.get(inv.id)' in body
