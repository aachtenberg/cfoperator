"""Tier 0 of the triage eval (docs/triage-eval-v2-plan.md, CFOP-314): the
harness and production agree on what they send and how they read the answer.

The harness scored a one-line system prompt for five days without noticing
(CFOP-313 turned the prompt literal into an expression and the harness's
regex captured its first string). These tests drive the real run_triage with
a capturing stub and hold the harness to the bytes production sent: system
prompt, user message (with and without precedents) and the /api/chat body
on the triage-model path. Then the gate itself: a v1-shaped result set
fails it, the committed v5 soak passes it, and the run count carries the
power the plan asks for.
"""

import importlib.util
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    'triage_eval', os.path.join(_ROOT, 'benchmarks', 'triage_eval.py'))
te = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(te)

V5_SOAK = os.path.join(_ROOT, 'benchmarks', 'triage_eval_cfop_triage_ministral3_v5_q4_soak.json')


def _operator():
    op = CFOperator.__new__(CFOperator)
    op.config = {'chat': {'max_tool_iterations': 4}, 'llm': {'primary': {}}}
    op.llm_timeout = 5
    op.embeddings = MagicMock()
    op.embeddings.is_available.return_value = False
    op.kb = SimpleNamespace(get_setting=lambda *a, **k: '', _kb=MagicMock())
    op.tools = SimpleNamespace(get_schemas=lambda policy=None: [], execute=lambda name, args: {})
    op.llm = SimpleNamespace(record_success=lambda *a: None, record_failure=lambda *a: None,
                             classify_error=lambda *a, **k: 'connection')
    return op


def _case(name):
    return te.CASES_BY_NAME[name]


def _alert_for(case):
    return {'severity': case['severity'], 'summary': case['summary'],
            'labels': case['labels'], 'details': {}, 'alert_id': 'c-1'}


def _capture_chain(op):
    seen = {}

    def chat(**kwargs):
        seen.update(kwargs)
        return {'response': '{"action": "investigate", "reason": "x", "confidence": 0.7}',
                'tool_calls': 0, 'backend': 'ollama', 'model': 'm'}
    op._chat_with_tools_with_fallback = chat
    return seen


# ---- the prompt ---------------------------------------------------------------

def test_the_system_prompt_is_what_run_triage_sends():
    op = _operator()
    seen = _capture_chain(op)
    op.run_triage(_alert_for(_case('novel-oom')))
    assert seen['system_context'] == te.production_system_prompt()
    # And it is the whole prompt, not the first string of the expression.
    assert 'Action rubric' in te.production_system_prompt()
    assert 'escalate' in te.production_system_prompt()


def test_the_user_message_matches_without_precedents():
    op = _operator()
    seen = _capture_chain(op)
    case = _case('novel-oom')
    op.run_triage(_alert_for(case))
    assert seen['messages'] == [{'role': 'user', 'content': te.build_user_message(case)}]


def test_the_user_message_matches_with_precedents():
    """The KB hands run_triage dicts; the case carries tuples. Both must land
    as the same framed block."""
    op = _operator()
    seen = _capture_chain(op)
    case = _case('known-sdcard')
    op.embeddings.is_available.return_value = True
    op.embeddings.generate_embedding.return_value = [0.0]
    op.kb._kb.find_similar_investigations_hybrid.return_value = [
        {'outcome': outcome, 'trigger': trigger, 'similarity': sim}
        for outcome, trigger, sim in case['similar']]
    op.run_triage(_alert_for(case))
    assert seen['messages'][0]['content'] == te.build_user_message(case)
    assert 'Similar past investigations' in te.build_user_message(case)


def test_the_request_body_is_what_the_triage_model_path_sends(monkeypatch):
    """The dedicated-triage-model branch posts straight to Ollama; the harness
    body must be that body, key for key (CFOP-279: no `options` unless
    num_ctx is set, on both sides)."""
    op = _operator()
    op._triage_model = lambda: 'cfop-triage-ministral3:v5-q4'
    op._triage_url = lambda: 'http://triage.test:11434'
    posted = {}

    def post(url, json=None, headers=None, timeout=None):
        posted['url'] = url
        posted['json'] = json
        body = {'message': {'role': 'assistant',
                            'content': '{"action": "investigate", "reason": "paperless-ngx OOM, no precedent", "confidence": 0.7}'},
                'done_reason': 'stop', 'prompt_eval_count': 10, 'eval_count': 5}
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: body)
    monkeypatch.setattr('requests.post', post)

    case = _case('novel-oom')
    decision = op.run_triage(_alert_for(case))
    assert decision['action'] == 'investigate' and decision['model'] == 'cfop-triage-ministral3:v5-q4'
    assert posted['url'] == 'http://triage.test:11434/api/chat'

    harness = te.build_payload('cfop-triage-ministral3:v5-q4', te.production_system_prompt(),
                               te.build_user_message(case))
    sent = dict(posted['json'])
    # The tool loop withholds tools on a one-shot call; nothing else may differ.
    sent.pop('tools', None)
    assert sent == harness, json.dumps({'sent': sent, 'harness': harness}, indent=1, default=str)[:3000]


def test_num_ctx_puts_sampling_options_in_the_body_like_production():
    op = _operator()
    op.config = {'llm': {'primary': {'num_ctx': 8192}}}
    production = op._ollama_chat_payload('m', [{'role': 'user', 'content': 'x'}], te.TRIAGE_TEMPERATURE)
    harness = te.build_payload('m', '', 'x', num_ctx=8192)
    assert harness['options'] == production['options'] == {'num_ctx': 8192, 'temperature': te.TRIAGE_TEMPERATURE}


# ---- the parser ---------------------------------------------------------------

def test_a_tool_calls_dump_is_not_a_decision():
    """Ministral's failure mode (ollama#16934): the tool-call syntax dumped as
    text. It must read as unparseable so the standard chain is consulted."""
    assert CFOperator._parse_triage_response(
        '[TOOL_CALLS][{"name": "classify", "arguments": {"action": "notify"}}]') is None


def test_grounded_means_the_alert_not_the_frame():
    """The framed prompt always contains "alert", "severity" and "labels";
    grounding is judged against the alert's own words (review on #336)."""
    case = _case('novel-oom')
    prompt, alert_text = te.build_user_message(case), te.alert_text_of(case)
    assert te.grade_reason('nothing resembles a known pattern here', prompt, alert_text)[0] is False
    assert te.grade_reason('paperless-ngx OOMKilled with no precedent', prompt, alert_text)[0] is True
    # Fabrication still looks at the whole prompt: a name from the frame's
    # precedent block is a real citation, not an invention.
    case2 = _case('known-sdcard')
    g, fab = te.grade_reason('raspberrypi3 SD card, replacement scheduled',
                             te.build_user_message(case2), te.alert_text_of(case2))
    assert g is True and fab == []


def test_an_errored_run_breaches_the_latency_gate(monkeypatch):
    """A call that never answered is not under the SLO; it must not drop out
    of the percentiles and leave the gate green."""
    monkeypatch.setenv('CFOP_TRIAGE_TIMEOUT_SECONDS', '120')
    rows = _rows(reason=lambda c, r: f'pod-{c} {r}', confidence=lambda c, r: 0.5 + 0.01 * (r % 3))
    rows[5]['error'] = 'TimeoutError: timed out'
    rows[5]['valid'] = False
    rows[5]['correct'] = False
    g = te.grade(rows, te.CASES_BY_NAME)
    assert g['latency']['per_case']['case-0']['errors'] == 1
    assert 'case-0' in g['latency']['p95_breaches']
    assert g['gate']['latency_p95_under_timeout'] is False


# ---- the gate -----------------------------------------------------------------

def _rows(n_cases=3, runs=36, *, reason, confidence, latency=1.0, correct=True):
    rows = []
    for c in range(n_cases):
        name = f'case-{c}'
        for r in range(runs):
            rows.append({'case': name, 'run': r, 'valid': True, 'action': 'notify',
                         'expected': ['notify'], 'correct': correct, 'latency_s': latency,
                         'confidence': confidence(c, r) if callable(confidence) else confidence,
                         'reason': reason(c, r) if callable(reason) else reason,
                         'reason_grounded': True, 'reason_fabricated': [], 'error': None})
    return rows


def test_a_v1_shaped_result_set_fails_the_gate(monkeypatch):
    """What the v1 fine-tune shipped: four template strings and a confidence
    that is a lookup of the action. 42/42 on action, and it must fail."""
    monkeypatch.setenv('CFOP_TRIAGE_TIMEOUT_SECONDS', '120')
    rows = _rows(reason='Similar past investigation resolved with little effort.', confidence=0.85)
    g = te.grade(rows, te.CASES_BY_NAME)
    assert g['action_accuracy'] == 1.0
    assert g['gate']['reason_none_canned'] is False
    assert g['gate']['confidence_not_action_alias'] is False
    assert g['gate']['pass'] is False


def test_a_per_alert_reason_with_spread_confidence_passes(monkeypatch):
    monkeypatch.setenv('CFOP_TRIAGE_TIMEOUT_SECONDS', '120')
    rows = _rows(reason=lambda c, r: f'pod-{c}: resolved precedent at 0.9{r % 3}',
                 confidence=lambda c, r: 0.6 + 0.01 * (r % 4))
    g = te.grade(rows, te.CASES_BY_NAME)
    assert g['gate']['pass'] is True, g['gate']
    assert g['runs_per_case'] == 36
    assert g['detection_power_at_reference_rate'] >= 0.95


def test_the_latency_gate_reads_the_deployed_timeout(monkeypatch):
    rows = _rows(reason=lambda c, r: f'pod-{c} {r}', confidence=lambda c, r: 0.5 + 0.01 * (r % 3),
                 latency=6.0)
    monkeypatch.delenv('CFOP_TRIAGE_TIMEOUT_SECONDS', raising=False)
    g = te.grade(rows, te.CASES_BY_NAME)
    assert g['latency']['timeout_s'] == 5.0 and 'unset' in g['latency']['timeout_source']
    assert g['gate']['latency_p95_under_timeout'] is False
    monkeypatch.setenv('CFOP_TRIAGE_TIMEOUT_SECONDS', '120')
    g = te.grade(rows, te.CASES_BY_NAME)
    assert g['gate']['latency_p95_under_timeout'] is True
    assert g['latency']['would_breach_unset_default'] is True


def test_the_committed_v5_soak_passes_everything_the_rows_can_show(monkeypatch):
    """v5-q4 ×36-class soak (100 runs over two cases): per-alert reasons, four
    confidence values within `investigate`, 0 fabricated."""
    monkeypatch.setenv('CFOP_TRIAGE_TIMEOUT_SECONDS', '120')
    g = te.replay(V5_SOAK)
    assert g['model'] == 'cfop-triage-ministral3:v5-q4'
    assert g['reason']['canned_runs'] == 0
    assert g['confidence']['is_action_alias'] is False
    assert g['gate']['pass'] is True, g['gate']


def test_power_is_what_the_plan_promises():
    assert te.detection_power(3) == pytest.approx(0.22, abs=0.01)
    assert te.detection_power(te.DEFAULT_RUNS) >= 0.95
    assert te.detection_power(0) == 0
