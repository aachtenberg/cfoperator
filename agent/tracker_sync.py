"""The tracker hand-off tick (CFOP-170).

Runs on the remediation worker thread beside reap / drain / verify, gated by
the ``queue_tracker`` flag. Reconciliation-style: it lists rows whose tracker
state has drifted from their status and acts on each, so a restart or a dead
service loses nothing — the next tick repeats only the un-acked actions.

Why a row leaves the active list. A ``needs-human`` row with no PR is not
work the system will do; it is a note for a person, and while it sits in the
console someone opens it and iterates with the LLM again (operator, 2026-09-09).
So filing it moves the row to ``filed``: handed off, groomed or discarded in
the tracker's backlog, and the outcome flows back here. Rows with a PR get an
item too (priority high, linked to the PR) but keep their status — the PR
reconciler still owns them.

Functions take the operator (``op``) rather than living on it, the pattern
``_dispatch_checklist_followup`` set: the decision table is pure and testable
against a MagicMock, and agent.py grows four small methods instead of a page.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from prometheus_client import Counter

from tracker_client import (
    TrackerClientError,
    comment_item as tracker_comment,
    create_item as tracker_create,
    get_item as tracker_get,
    transition_item as tracker_transition,
)
from tracker_item import (
    build_item,
    build_pr_comment,
    build_queued_comment,
    build_reparked_comment,
    build_transition_note,
)

logger = logging.getLogger(__name__)

# action: create | repark | refile | comment | transition | reconcile; outcome:
# ok | error. ``reconcile`` counts only polls that changed the row (issue
# done/cancelled); a poll that found the item still open is not an action.
REMEDIATION_TRACKER = Counter(
    'cfoperator_remediation_tracker_total',
    'Issue-tracker hand-off actions on remediation rows (CFOP-170)',
    ['action', 'outcome'])


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def tracker_state(row: Dict[str, Any]) -> Dict[str, Any]:
    res = row.get('result')
    tr = res.get('tracker') if isinstance(res, dict) else None
    return dict(tr) if isinstance(tr, dict) else {}


def _has_pr(row: Dict[str, Any]) -> bool:
    return bool(str(row.get('pr_url') or '').strip())


def decide(row: Dict[str, Any]) -> Optional[str]:
    """Which tracker action this row needs now, or None.

    The whole lifecycle table in one place, so a test can pin each line of it
    without HTTP: what a row without a ref may trigger (only a create, and only
    while parked or PR-open — never post-hoc), and what a row with a ref owes
    the item when its status moved.
    """
    status = row.get('status')
    tr = tracker_state(row)
    ref = tr.get('ref')
    synced = tr.get('synced_status')
    if not ref:
        return 'create' if status in ('needs-human', 'pr-open') else None
    if status == 'filed':
        return 'reconcile'
    if status == 'needs-human':
        # A filed row that was approved and parked again, or a PR-open row
        # whose executor pass declined. Either way the item learns why, and a
        # row without a PR goes back to being handed off. ``refile`` is the
        # second half alone: the note already landed (synced says filed) but
        # the status write did not, so only the write is owed — the same
        # note must not be posted again on every tick.
        if _has_pr(row):
            return 'comment' if synced != status else None
        return 'refile' if synced == 'filed' else 'repark'
    if status in ('pr-open', 'queued'):
        return 'comment' if synced != status else None
    if status in ('resolved', 'rejected'):
        res = row.get('result') if isinstance(row.get('result'), dict) else {}
        if synced in ('resolved', 'rejected') or res.get('resolved_by') == 'tracker':
            return None
        return 'transition'
    return None


def sync_tracker(op) -> int:
    """One tick. Returns the number of rows acted on; never raises."""
    if not op._remediation_flag('queue_tracker'):
        return 0
    base = op._tracker_url()
    if not base:
        if not getattr(op, '_tracker_url_warned', False):
            logger.warning("remediation.queue_tracker is on but no tracker URL is set "
                           "(CFOP_TRACKER_URL or remediation.tracker.url); the hand-off is a no-op")
            op._tracker_url_warned = True
        return 0
    rcfg = op.config.get('remediation', {}) if isinstance(op.config, dict) else {}
    try:
        limit = max(1, int(rcfg.get('max_tracker_per_tick') or 10))
    except (TypeError, ValueError):
        limit = 10
    try:
        rows = op.kb.list_remediations_for_tracker(limit=limit)
    except Exception as e:
        logger.error(f"Tracker sync list failed: {e}", exc_info=True)
        return 0
    acted = 0
    for row in rows or []:
        try:
            acted += sync_tracker_row(op, row, base)
        except Exception:  # belt and braces; sync_tracker_row already isolates
            logger.exception("Tracker sync failed for remediation #%s", (row or {}).get('id'))
    if acted:
        logger.info(f"Tracker hand-off: {acted} action(s)")
    return acted


def sync_tracker_row(op, row: Dict[str, Any], base: str) -> int:
    """Act on one row per ``decide``. Returns 1 when the row changed, else 0."""
    action = decide(row)
    if not action:
        return 0
    rid = row.get('id')
    status = row.get('status')
    tr = tracker_state(row)
    ref = tr.get('ref')
    now = _now()
    try:
        if action == 'create':
            item = build_item(row, console_base_url=op._console_base_url())
            got = tracker_create(base, item)
            tr = {'backend': got.get('backend'), 'ref': got['ref'], 'url': got.get('url'),
                  'key': got.get('key'), 'synced_status': status, 'synced_at': now,
                  'error': None, 'error_count': 0}
            if status == 'needs-human' and not _has_pr(row):
                # The hand-off: the row leaves the active list. The very next
                # statement after a successful create is this write — nothing
                # may sit between them, or a crash files the item twice.
                tr['synced_status'] = 'filed'
                op.kb.update_remediation_status(rid, 'filed', result={'tracker': tr})
                logger.info(f"Remediation #{rid} filed to the tracker as {tr.get('key')} (handed off)")
            else:
                op.kb.merge_remediation_result(rid, {'tracker': tr})
                logger.info(f"Remediation #{rid} ({status}) mirrored to the tracker as {tr.get('key')}")
        elif action == 'repark':
            tracker_comment(base, ref, build_reparked_comment(row))
            tr.update(synced_status='filed', synced_at=now, error=None, error_count=0)
            op.kb.update_remediation_status(rid, 'filed', result={'tracker': tr})
        elif action == 'refile':
            tr.update(synced_at=now, error=None, error_count=0)
            op.kb.update_remediation_status(rid, 'filed', result={'tracker': tr})
        elif action == 'comment':
            text = build_queued_comment(row) if status == 'queued' else build_pr_comment(row)
            tracker_comment(base, ref, text)
            tr.update(synced_status=status, synced_at=now, error=None, error_count=0)
            op.kb.merge_remediation_result(rid, {'tracker': tr})
        elif action == 'transition':
            tracker_transition(base, ref, status, build_transition_note(row, status))
            tr.update(synced_status=status, synced_at=now, error=None, error_count=0)
            op.kb.merge_remediation_result(rid, {'tracker': tr})
        elif action == 'reconcile':
            changed = _reconcile_filed_row(op, row, base, tr, now)
            if not changed:
                return 0
        REMEDIATION_TRACKER.labels(action=action, outcome='ok').inc()
        return 1
    except Exception as e:  # TrackerClientError, KB errors, anything — the tick must go on
        REMEDIATION_TRACKER.labels(action=action, outcome='error').inc()
        logger.warning(f"Tracker {action} failed for remediation #{rid}: {e}")
        tr.update(error=str(e)[:500], error_at=now,
                  error_count=int(tr.get('error_count') or 0) + 1)
        try:
            op.kb.merge_remediation_result(rid, {'tracker': tr})
        except Exception as e2:
            logger.error(f"Could not record tracker error on remediation #{rid}: {e2}")
        return 0


def _reconcile_filed_row(op, row: Dict[str, Any], base: str, tr: Dict[str, Any], now: str) -> bool:
    """Poll a filed row's item; close the row when the tracker closed the item.

    Returns True when the row changed. The tracker write and the row write are
    one KB call with ``synced_status`` already at the new status, so the next
    tick does not see drift and bounce a transition back at the tracker.
    """
    rid = row.get('id')
    key = tr.get('key') or tr.get('ref')
    got = tracker_get(base, tr['ref'])
    if got is None:
        # Deleted in the tracker: the backlog was groomed and this item was
        # thrown away. That is a rejection, and the row says so.
        tr.update(synced_status='rejected', synced_at=now, checked_at=now, error=None, error_count=0,
                  closed_by='tracker', tracker_state='deleted')
        op.kb.update_remediation_status(rid, 'rejected', last_error=f"item {key} deleted in the tracker",
                                        result={'tracker': tr})
        _count_outcome('rejected')
        return True
    state = str(got.get('state') or 'open')
    if state == 'resolved':
        tr.update(synced_status='resolved', synced_at=now, checked_at=now, error=None, error_count=0,
                  closed_by='tracker')
        op.kb.update_remediation_status(
            rid, 'resolved',
            result={'resolved_by': 'tracker', 'resolution_note': f"closed as done in the tracker ({key})",
                    'tracker': tr})
        _count_outcome('resolved')
        logger.info(f"Remediation #{rid} resolved: {key} closed in the tracker")
        return True
    if state == 'rejected':
        tr.update(synced_status='rejected', synced_at=now, checked_at=now, error=None, error_count=0,
                  closed_by='tracker')
        op.kb.update_remediation_status(rid, 'rejected', last_error=f"cancelled in the tracker ({key})",
                                        result={'tracker': tr})
        _count_outcome('rejected')
        logger.info(f"Remediation #{rid} rejected: {key} cancelled in the tracker")
        return True
    # Still open: stamp the poll so the next tick looks at another row first.
    tr.update(checked_at=now, error=None)
    op.kb.merge_remediation_result(rid, {'tracker': tr})
    return False


def _count_outcome(outcome: str) -> None:
    """Terminal outcomes share the PR reconciler's counter (dashboards key off it)."""
    try:
        from agent.agent import REMEDIATION_OUTCOME
        REMEDIATION_OUTCOME.labels(outcome=outcome).inc()
    except Exception:  # noqa: BLE001 - import cycle in odd test layouts; the tracker counter still counts
        pass
