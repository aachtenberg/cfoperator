#!/usr/bin/env python3
"""The cockpit janitor tick on the agent's worker thread (CFOP-36).

Kubernetes reaps tier 1 for nothing — ``activeDeadlineSeconds`` plus ownership
GC. A container or a ``/tmp`` directory on a Pi has no such machinery, and the
sessions that leak are exactly the ones nobody is watching: the laptop that
closed, the VPN that dropped. So the agent sweeps.

These guard the *wiring*, not the sweep itself (``test_cockpit_ladder.py`` owns
that): the tick has to run on the remediation worker thread rather than the
OODA loop, it has to be cheap and silent on an install with no SSH fleet, and
it must never raise — an exception here would take the drainer's tick with it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator


def _operator(*, web_server=None, config=None, setting=None):
    op = CFOperator.__new__(CFOperator)
    op.config = config if config is not None else {}
    op.web_server = web_server

    class _KB:
        def get_setting(self, name, default=''):
            if setting is None:
                raise RuntimeError("settings table unavailable")
            return setting

    op.kb = _KB()
    return op


class _WebServer:
    def __init__(self, result=3):
        self.result = result
        self.calls = 0

    def reap_cockpits(self):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


# --- the tick ---------------------------------------------------------------

def test_the_tick_delegates_to_the_console_ladder():
    ws = _WebServer(result=2)
    assert _operator(web_server=ws)._reap_cockpits() == 2
    assert ws.calls == 1


def test_an_install_without_a_console_never_sweeps():
    """The web server is optional (it can be disabled in config), and the
    ladder lives on it. No console means no cockpits to reap."""
    assert _operator(web_server=None)._reap_cockpits() == 0


def test_a_failing_sweep_never_reaches_the_worker_loop():
    """It ticks beside the remediation reaper, drainer and PR reconcile. An
    exception escaping here would take that whole thread's iteration with it,
    and the drainer is the one that actually ships fixes."""
    ws = _WebServer(result=RuntimeError("every host is unreachable"))
    assert _operator(web_server=ws)._reap_cockpits() == 0
    assert ws.calls == 1


# --- the interval -----------------------------------------------------------

def test_the_interval_prefers_the_live_console_setting():
    """DB over config, the same precedence every other interval uses — so it
    can be changed without a redeploy mid-incident."""
    assert _operator(setting='60')._get_cockpit_reap_interval() == 60


def test_the_interval_falls_back_to_config_then_a_default():
    assert _operator()._get_cockpit_reap_interval() == 900
    assert _operator(config={'cockpit': {'janitor_interval_seconds': 300}}
                     )._get_cockpit_reap_interval() == 300


def test_a_junk_setting_does_not_wedge_the_sweep():
    """A setting of 0 would spin the sweep on every worker iteration — an ssh
    storm across the fleet — and a negative one is the same thing."""
    assert _operator(setting='0')._get_cockpit_reap_interval() == 60
    assert _operator(setting='not-a-number')._get_cockpit_reap_interval() == 900
    assert _operator(setting='999999999')._get_cockpit_reap_interval() == 86400


# --- where it runs ----------------------------------------------------------

def test_the_tick_is_on_the_worker_thread_not_the_ooda_loop():
    """A proactive sweep is minutes long and single-threaded; a janitor tick
    behind it would run once an hour instead of every fifteen minutes. Same
    reason the drainer moved off that loop."""
    import inspect

    loop = inspect.getsource(CFOperator._remediation_worker_loop)
    assert '_reap_cockpits()' in loop
    assert '_get_cockpit_reap_interval()' in loop
