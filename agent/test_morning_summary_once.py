"""The morning summary runs once per day, across pod restarts.

``_check_morning_summary`` used to remember "sent today" only on the operator
object, so every deploy inside the summary window re-ran the report
(2026-08-28: three summaries, the second and third from back-to-back
rollouts, each feeding duplicate rows into the remediation queue). The marker
now also lives in agent settings. A fresh operator built against the same
fake settings store stands in for a restarted pod.
"""
from datetime import date

from agent import CFOperator

# The agent-settings key is a DB contract; pin it here so a rename shows up.
_MORNING_SUMMARY_SENT_SETTING = 'morning_summary_sent_on'


class _KB:
    """Stand-in for the resilient KB: settings + the sweep-report store."""

    def __init__(self, offline=False):
        self.values = {}
        self.offline = offline
        self.reports = []

    def get_setting(self, key, default=None):
        return self.values.get(key, default)

    def set_setting(self, key, value):
        if self.offline:
            raise ConnectionError("Database is offline")
        self.values[key] = value

    def store_sweep_report(self, **kw):
        self.reports.append(kw)


def _operator(kb, hour_start=0, hour_end=24):
    op = CFOperator.__new__(CFOperator)
    op.config = {'ooda': {'morning_summary': {
        'enabled': True, 'hour_start': hour_start, 'hour_end': hour_end}}}
    op.kb = kb
    op.notifications = []
    op.generated = 0

    def _generate():
        op.generated += 1
        return {'text': 'overnight report', 'severity': 'info'}

    op._generate_morning_summary = _generate
    return op


def test_first_run_generates_once_and_persists_the_date():
    kb = _KB()
    op = _operator(kb)
    op._check_morning_summary()
    op._check_morning_summary()
    assert op.generated == 1
    assert kb.values[_MORNING_SUMMARY_SENT_SETTING] == date.today().isoformat()
    assert len(kb.reports) == 1


def test_restarted_pod_does_not_rerun_today():
    kb = _KB()
    _operator(kb)._check_morning_summary()          # the pod that sent it
    restarted = _operator(kb)                        # no in-memory state
    restarted._check_morning_summary()
    assert restarted.generated == 0
    assert len(kb.reports) == 1


def test_stale_marker_from_a_previous_day_does_not_block():
    kb = _KB()
    kb.values[_MORNING_SUMMARY_SENT_SETTING] = '2000-01-01'
    op = _operator(kb)
    op._check_morning_summary()
    assert op.generated == 1
    assert kb.values[_MORNING_SUMMARY_SENT_SETTING] == date.today().isoformat()


def test_offline_db_still_sends_once_per_process():
    kb = _KB(offline=True)
    op = _operator(kb)
    op._check_morning_summary()                      # set_setting raises; must not propagate
    op._check_morning_summary()
    assert op.generated == 1
    assert _MORNING_SUMMARY_SENT_SETTING not in kb.values


def test_outside_the_window_nothing_happens():
    kb = _KB()
    op = _operator(kb, hour_start=0, hour_end=0)     # empty window
    op._check_morning_summary()
    assert op.generated == 0
    assert kb.values == {}
