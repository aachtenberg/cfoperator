"""The whole-chain LLM circuit breaker (CFOP-321).

Per-provider cooldowns never stopped a chain walk: with the network gone or
every key exhausted, each call still tried every rung with its own timeout.
These pin the breaker's state machine, its wiring into
_chat_with_tools_with_fallback (an open circuit asks no provider), and what an
investigation records when it is refused.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest
import requests
from prometheus_client import REGISTRY

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator
from llm_fallback import (
    CIRCUIT_CLOSED, CIRCUIT_HALF_OPEN, CIRCUIT_OPEN,
    ChainCircuitBreaker, LLMCircuitOpen, LLMFallbackManager,
)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _breaker(clock, threshold=3, window=1800, cooldown=300):
    return ChainCircuitBreaker(failure_threshold=threshold, window_seconds=window,
                               cooldown_seconds=cooldown, clock=clock)


# --- state machine ------------------------------------------------------------

def test_opens_after_the_threshold_and_fails_fast_with_the_reopen_time():
    clock = Clock()
    b = _breaker(clock)
    assert b.record_failure() is False
    assert b.record_failure() is False
    assert b.record_failure() is True
    assert b.state == CIRCUIT_OPEN
    with pytest.raises(LLMCircuitOpen, match="LLM circuit open until "):
        b.before_call()


def test_failures_outside_the_window_do_not_count():
    clock = Clock()
    b = _breaker(clock, window=1800)
    b.record_failure()
    b.record_failure()
    clock.t += 1801
    assert b.record_failure() is False
    assert b.state == CIRCUIT_CLOSED


def test_a_success_between_failures_resets_the_count():
    clock = Clock()
    b = _breaker(clock)
    b.record_failure()
    b.record_failure()
    b.record_success()
    assert b.record_failure() is False


def test_closed_open_half_open_closed():
    clock = Clock()
    b = _breaker(clock, cooldown=300)
    for _ in range(3):
        b.record_failure()
    clock.t += 299
    with pytest.raises(LLMCircuitOpen):
        b.before_call()
    clock.t += 2
    b.before_call()  # the probe is let through
    assert b.state == CIRCUIT_HALF_OPEN
    b.record_success()
    assert b.state == CIRCUIT_CLOSED
    b.before_call()


def test_a_failed_probe_reopens_for_another_cooldown():
    clock = Clock()
    b = _breaker(clock, cooldown=300)
    for _ in range(3):
        b.record_failure()
    clock.t += 301
    b.before_call()
    assert b.record_failure() is True, "a failed probe re-opens on its own, no threshold"
    assert b.state == CIRCUIT_OPEN
    clock.t += 299
    with pytest.raises(LLMCircuitOpen):
        b.before_call()


def test_only_one_probe_at_a_time():
    clock = Clock()
    b = _breaker(clock)
    for _ in range(3):
        b.record_failure()
    clock.t += 301
    b.before_call()
    with pytest.raises(LLMCircuitOpen, match="probe"):
        b.before_call()


def test_a_released_probe_lets_the_next_caller_probe():
    clock = Clock()
    b = _breaker(clock)
    for _ in range(3):
        b.record_failure()
    clock.t += 301
    b.before_call()
    b.release_probe()
    b.before_call()


def test_threshold_zero_disables_it():
    b = _breaker(Clock(), threshold=0)
    for _ in range(10):
        assert b.record_failure() is False
    b.before_call()


def test_config_defaults_and_bad_values():
    b = ChainCircuitBreaker.from_config(None)
    assert (b.failure_threshold, b.window_seconds, b.cooldown_seconds) == (3, 1800, 300)
    b = ChainCircuitBreaker.from_config({'failure_threshold': 5, 'cooldown_seconds': 60})
    assert (b.failure_threshold, b.window_seconds, b.cooldown_seconds) == (5, 1800, 60)
    b = ChainCircuitBreaker.from_config({'failure_threshold': 'lots', 'window_seconds': -1,
                                         'cooldown_seconds': True})
    assert (b.failure_threshold, b.window_seconds, b.cooldown_seconds) == (3, 1800, 300)


def test_the_manager_always_carries_a_breaker():
    mgr = LLMFallbackManager(db_session_factory=_no_db, settings_getter=dict)
    assert isinstance(mgr.circuit, ChainCircuitBreaker)


# --- wiring into the chain walk -----------------------------------------------

def _no_db():
    raise RuntimeError("no database in this test")


CHAIN = [("ollama", "http://gpu:11434", "gemma"), ("groq", None, "llama")]


def _operator(breaker, responses):
    """A bare CFOperator whose chain walk answers from ``responses``.

    Each item is a result dict or an exception raised for that attempt.
    Returns (op, calls) where calls lists the providers actually asked.
    """
    op = CFOperator.__new__(CFOperator)
    mgr = LLMFallbackManager.__new__(LLMFallbackManager)
    mgr.db_session_factory = _no_db  # record_* swallow the failure
    mgr.get_settings = dict
    mgr.circuit = breaker
    op.llm = mgr
    op._get_provider_chain = lambda backend='auto', model=None: list(CHAIN)
    op._skipped_selection = lambda backend, head: None
    calls, it = [], iter(responses)

    def chat(provider_type, url, model, messages, system_context,
             max_iterations=None, event_callback=None, tool_policy=None):
        calls.append(provider_type)
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return dict(item)
    op._chat_with_tools = chat
    return op, calls


def _walk(op):
    return op._chat_with_tools_with_fallback(messages=[{'role': 'user', 'content': 'x'}])


def _metric(name):
    return REGISTRY.get_sample_value(name) or 0.0


def test_an_open_circuit_asks_no_provider_at_all():
    clock = Clock()
    down = requests.ConnectionError("connection refused")
    op, calls = _operator(_breaker(clock), [down] * 6)
    opens_before = _metric('cfoperator_llm_circuit_opens_total')
    for _ in range(3):
        with pytest.raises(requests.ConnectionError):
            _walk(op)
    assert len(calls) == 6
    assert _metric('cfoperator_llm_circuit_opens_total') == opens_before + 1
    assert _metric('cfoperator_llm_circuit_state') == 1
    with pytest.raises(LLMCircuitOpen):
        _walk(op)
    assert len(calls) == 6, "an open circuit must not walk the chain"


def test_the_probe_that_succeeds_closes_the_circuit():
    clock = Clock()
    down = requests.ConnectionError("connection refused")
    op, calls = _operator(_breaker(clock), [down] * 6 + [{'response': 'ok'}])
    for _ in range(3):
        with pytest.raises(requests.ConnectionError):
            _walk(op)
    clock.t += 301
    assert _walk(op)['response'] == 'ok'
    assert op.llm.circuit.state == CIRCUIT_CLOSED
    assert _metric('cfoperator_llm_circuit_state') == 0


def test_every_rung_rejecting_the_request_is_not_a_chain_outage():
    """A 400 from every provider is about the request (a context too large, a
    payload bug), and must not lock the console chat out for everyone."""
    clock = Clock()
    rejected = RuntimeError("Error code: 400 - invalid_request_error")
    op, calls = _operator(_breaker(clock), [rejected] * 8)
    for _ in range(4):
        with pytest.raises(RuntimeError, match="400"):
            _walk(op)
    assert op.llm.circuit.state == CIRCUIT_CLOSED
    assert len(calls) == 8


def test_one_rung_down_among_rejections_still_counts():
    clock = Clock()
    mixed = [requests.Timeout("read timed out"), RuntimeError("Error code: 400")] * 3
    op, _ = _operator(_breaker(clock), mixed)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            _walk(op)
    assert op.llm.circuit.state == CIRCUIT_OPEN


# --- what an investigation records ---------------------------------------------

def test_a_refused_investigation_is_recorded_failed_with_the_reason():
    captured = {}
    op = CFOperator.__new__(CFOperator)
    op.config = {}
    op.kb = SimpleNamespace(
        start_investigation=lambda trigger, alert_id=None: 91,
        update_investigation=lambda **kw: captured.update(kw) or True)
    op._noise_config = lambda: {'enabled': False}

    def refused(**kw):
        raise LLMCircuitOpen(2_000_000_000.0, "the provider chain kept failing")
    op._chat_with_tools_with_fallback = refused
    result = op._act({'trigger': 'loki-0 restarting', 'alert': {},
                      'known_learnings': [], 'similar_investigations': []})
    assert captured['outcome'] == 'failed'
    assert captured['findings']['error'].startswith('LLM circuit open until ')
    assert result['details']['error'] == 'llm_circuit_open'
