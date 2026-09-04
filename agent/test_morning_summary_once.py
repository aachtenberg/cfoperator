"""The morning summary runs once per day, across pod restarts.

``_check_morning_summary`` used to remember "sent today" only on the operator
object, so every deploy inside the summary window re-ran the report
(2026-08-28: three summaries, the second and third from back-to-back
rollouts, each feeding duplicate rows into the remediation queue). The marker
now also lives in agent settings. A fresh operator built against the same
fake settings store stands in for a restarted pod.

The read side fails *closed* and the write side fails *open*, which is not
symmetry for its own sake: generating is what feeds the queue, so an
untrustworthy read must skip the tick, while a marker that cannot be written
only costs restart-safety for the day.
"""
from datetime import date

from agent import CFOperator

# The agent-settings key is a DB contract; pin it here so a rename shows up.
_MORNING_SUMMARY_SENT_SETTING = 'morning_summary_sent_on'


class _KB:
    """Stand-in for the resilient KB: settings + the sweep-report store.

    Mirrors ``ResilientKnowledgeBase`` where it matters: ``get_setting``
    degrades *silently* to the default when the connection is unhealthy
    (it does not raise), ``set_setting`` raises, and ``is_online()`` is the
    only way to tell an empty read from a missing database.
    """

    def __init__(self, online=True, write_fails=False, read_raises=False,
                 store_fails=False):
        self.values = {}
        self.online = online
        self.write_fails = write_fails
        self.read_raises = read_raises
        self.store_fails = store_fails
        self.reports = []

    def is_online(self):
        return self.online

    def get_setting(self, key, default=None):
        if self.read_raises:
            raise ConnectionError("connection reset")
        if not self.online:
            return default
        return self.values.get(key, default)

    def set_setting(self, key, value):
        if self.write_fails or not self.online:
            raise ConnectionError("Database is offline")
        self.values[key] = value

    def store_sweep_report(self, **kw):
        if self.store_fails:
            raise ConnectionError("Database is offline")
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


def test_unwritable_marker_still_sends_once_per_process():
    # Reads work, the write does not: best-effort, the summary must still go.
    kb = _KB(write_fails=True)
    op = _operator(kb)
    op._check_morning_summary()                      # set_setting raises; must not propagate
    op._check_morning_summary()
    assert op.generated == 1
    assert len(kb.reports) == 1
    assert _MORNING_SUMMARY_SENT_SETTING not in kb.values


def test_offline_db_skips_rather_than_running_unguarded():
    # The wrapper returns '' for every read when the health monitor is down,
    # which is indistinguishable from "no marker" — exactly the restart case
    # this issue is about. Skip and retry, do not generate.
    kb = _KB(online=False)
    kb.values[_MORNING_SUMMARY_SENT_SETTING] = date.today().isoformat()
    op = _operator(kb)
    op._check_morning_summary()
    assert op.generated == 0
    assert kb.reports == []
    # ...and once the DB is back, the marker is seen and today stays quiet.
    kb.online = True
    op._check_morning_summary()
    assert op.generated == 0


def test_offline_db_recovers_and_sends_when_no_marker_exists():
    kb = _KB(online=False)
    op = _operator(kb)
    op._check_morning_summary()
    assert op.generated == 0
    kb.online = True
    op._check_morning_summary()
    assert op.generated == 1
    assert kb.values[_MORNING_SUMMARY_SENT_SETTING] == date.today().isoformat()


def test_read_error_skips_the_tick():
    kb = _KB(read_raises=True)
    op = _operator(kb)
    op._check_morning_summary()
    assert op.generated == 0
    assert kb.reports == []


def test_failed_store_leaves_the_day_retryable():
    # The marker must not land before the digest does: a sticky marker means
    # the restart that used to retry skips instead, and nothing is ever stored.
    kb = _KB(store_fails=True)
    _operator(kb)._check_morning_summary()
    assert _MORNING_SUMMARY_SENT_SETTING not in kb.values
    kb.store_fails = False
    restarted = _operator(kb)
    restarted._check_morning_summary()
    assert restarted.generated == 1
    assert len(kb.reports) == 1
    assert kb.values[_MORNING_SUMMARY_SENT_SETTING] == date.today().isoformat()


def test_outside_the_window_nothing_happens():
    kb = _KB()
    op = _operator(kb, hour_start=0, hour_end=0)     # empty window
    op._check_morning_summary()
    assert op.generated == 0
    assert kb.values == {}


# ---- metrics wrapper (CFOP-163) --------------------------------------------

def test_summary_decorator_records_runs_and_the_last_success_time():
    import sys as _sys, pytest, time as _time
    M = _sys.modules[CFOperator.__module__]
    ok = M._meter_morning_summary(lambda self: {'text': 'overnight report', 'severity': 'info'})
    boom = M._meter_morning_summary(lambda self: (_ for _ in ()).throw(RuntimeError("database is offline")))
    ok_before = M.MORNING_SUMMARY_RUNS.labels(result='ok')._value.get()
    t0 = _time.time()
    assert ok(object())['text'] == 'overnight report'
    assert M.MORNING_SUMMARY_RUNS.labels(result='ok')._value.get() == ok_before + 1
    assert M.MORNING_SUMMARY_LAST_SUCCESS.labels(host_id='cfoperator')._value.get() >= t0
    err_before = M.MORNING_SUMMARY_RUNS.labels(result='error')._value.get()
    with pytest.raises(RuntimeError):
        boom(object())
    assert M.MORNING_SUMMARY_RUNS.labels(result='error')._value.get() == err_before + 1
    # And the real method is decorated, not just the helper.
    assert getattr(CFOperator._generate_morning_summary, '__wrapped__', None) is not None
