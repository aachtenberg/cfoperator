#!/usr/bin/env python3
"""TOOL_CALLS must record failures as result=error (CFOP-101).

The counter used to increment result='success' after every tool return, so
HighToolFailureRate's numerator was permanently zero. These tests induce a
failure through _dispatch_tool_call and assert the series that the alert
reads actually moved.

Each test uses its own tool_name label and asserts a delta, so it is immune
to increments from other tests sharing this process.
"""

import os
import sys
from unittest.mock import MagicMock

from prometheus_client import REGISTRY

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator  # noqa: E402
from agent.agent import _ToolLoopStats, _tool_call_result_label  # noqa: E402

_ALERT_THRESHOLD = 0.1
_METRICS_DOC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', 'docs', 'METRICS.md')


def _sample(tool_name, result):
    value = REGISTRY.get_sample_value(
        'cfoperator_tool_calls_total',
        {'tool_name': tool_name, 'result': result})
    return value or 0.0


def _dispatch(tool_name, result, *, cached=False, exec_error=None):
    op = MagicMock()
    stats = _ToolLoopStats()
    if exec_error is not None:
        op._cached_tool_exec.side_effect = exec_error
    else:
        op._cached_tool_exec.return_value = ('content', result, cached)
    return CFOperator._dispatch_tool_call(
        op, tool_name, {}, stats=stats, tool_cache={},
        max_result_chars=1000, iteration=0, max_iterations=5)


# --- classifier --------------------------------------------------------------

def test_success_true_is_success_even_with_an_error_key():
    assert _tool_call_result_label({'success': True, 'error': None}) == 'success'
    assert _tool_call_result_label({'success': True, 'result': []}) == 'success'


def test_success_false_is_error():
    assert _tool_call_result_label(
        {'success': False, 'error': 'Unknown host: nope'}) == 'error'


def test_truthy_error_key_without_success_true_is_error():
    assert _tool_call_result_label({'error': 'Prometheus backend not configured'}) == 'error'
    assert _tool_call_result_label({'error': 'Tool nope not found'}) == 'error'


def test_empty_read_without_error_is_success():
    assert _tool_call_result_label({'pods': []}) == 'success'
    assert _tool_call_result_label([{'id': 1}]) == 'success'


def test_error_string_payload_is_error():
    assert _tool_call_result_label('Error: tool crashed') == 'error'
    assert _tool_call_result_label('ok') == 'success'


# --- dispatch increments -----------------------------------------------------

def test_failed_tool_increments_error_not_success():
    tool = 'cfop101-ssh-unknown-host'
    before_e, before_s = _sample(tool, 'error'), _sample(tool, 'success')
    _dispatch(tool, {'success': False, 'error': 'Unknown host: nope'})
    assert _sample(tool, 'error') == before_e + 1
    assert _sample(tool, 'success') == before_s


def test_successful_tool_still_increments_success():
    tool = 'cfop101-k8s-empty-list'
    before_e, before_s = _sample(tool, 'error'), _sample(tool, 'success')
    _dispatch(tool, {'success': True, 'pods': []})
    assert _sample(tool, 'success') == before_s + 1
    assert _sample(tool, 'error') == before_e


def test_cached_repeat_keeps_the_original_result_label():
    # _cached_tool_exec returns the original result object as `result`, not
    # the stub, so a cached error stays an error.
    tool = 'cfop101-cached-error'
    before_e = _sample(tool, 'error')
    _dispatch(tool, {'error': 'timed out'}, cached=True)
    assert _sample(tool, 'error') == before_e + 1


def test_raise_escaping_exec_counts_as_error_then_propagates():
    tool = 'cfop101-serialize-boom'
    before_e = _sample(tool, 'error')
    try:
        _dispatch(tool, None, exec_error=RuntimeError('json dump failed'))
    except RuntimeError:
        pass
    else:
        raise AssertionError('expected RuntimeError to propagate')
    assert _sample(tool, 'error') == before_e + 1


# --- HighToolFailureRate against an induced window --------------------------
#
# Prometheus `rate()` over a window where the only new samples are errors
# equals error_delta / total_delta. The alert fires when that ratio > 0.1.
# We induce the failures through the real increment site, then evaluate the
# same inequality the rule uses. Reasoning about a counter that never moved
# is exactly how the inert rule survived.

def test_high_tool_failure_rate_fires_on_induced_failures():
    tool = 'cfop101-alert-probe'
    before_e, before_s = _sample(tool, 'error'), _sample(tool, 'success')
    for _ in range(5):
        _dispatch(tool, {'error': 'boom'})
    error_delta = _sample(tool, 'error') - before_e
    success_delta = _sample(tool, 'success') - before_s
    total = error_delta + success_delta
    assert error_delta == 5
    assert success_delta == 0
    assert total > 0
    assert (error_delta / total) > _ALERT_THRESHOLD


def test_high_tool_failure_rate_does_not_fire_on_a_lone_blip():
    tool = 'cfop101-alert-blip'
    before_e, before_s = _sample(tool, 'error'), _sample(tool, 'success')
    _dispatch(tool, {'error': 'once'})
    for _ in range(19):
        _dispatch(tool, {'success': True, 'ok': True})
    error_delta = _sample(tool, 'error') - before_e
    success_delta = _sample(tool, 'success') - before_s
    total = error_delta + success_delta
    assert error_delta == 1
    assert (error_delta / total) <= _ALERT_THRESHOLD


def test_metrics_doc_alert_is_live_and_lists_error():
    with open(_METRICS_DOC, encoding='utf-8') as fh:
        text = fh.read()
    assert 'HighToolFailureRate' in text
    assert 'inert' not in text.lower()
    assert 'cfoperator_tool_calls_total{result="error"}' in text
    assert '| `cfoperator_tool_calls_total` | `result` | `success`, `error` |' in text
    # The rule's threshold must stay the one the tests above evaluate.
    assert '> 0.1' in text.split('HighToolFailureRate', 1)[1][:400]


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-q']))
