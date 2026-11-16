"""A console chat turn says what it cost (CFOP-718).

The tool loop has always counted tokens, for Prometheus and the trace span.
The chat's ``done`` payload was built by hand and left them out, so the
browser and the stored transcript never saw them. These run the real
``handle_chat_message_stream`` over a scripted model: what reaches the console
is what the provider reported, the context figure is the last call's prompt
and not the sum, and a window nobody measured is not reported at all.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator  # noqa: E402
from agent import agent as agent_module  # noqa: E402
from agent.agent import _ToolLoopStats, _turn_usage  # noqa: E402


class _TwoCalls:
    """A tool call, then the answer: two model calls, each with its own
    prompt size, the second larger because it carries the tool's result."""

    def __init__(self):
        self.script = [
            {'message': {'role': 'assistant', 'content': '',
                         'tool_calls': [{'function': {'name': 'k8s_get_pods', 'arguments': {}}}]},
             'done_reason': 'stop', 'prompt_eval_count': 100, 'eval_count': 7},
            {'message': {'role': 'assistant', 'content': 'All good.'},
             'done_reason': 'stop', 'prompt_eval_count': 150, 'eval_count': 20},
        ]

    def __call__(self, url, json=None, headers=None, timeout=None):
        body = self.script.pop(0)
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: body)


def _operator(chain=(('ollama', 'http://up:11434', 'gemma4:26b'),), config=None):
    op = CFOperator.__new__(CFOperator)
    op.config = config or {'chat': {'max_tool_iterations': 3}}
    op.llm_timeout = 5
    op.tools = SimpleNamespace(get_schemas=lambda policy=None: [],
                               execute=lambda name, args, policy=None: {'pods': 3})
    op.kb = MagicMock()
    op.kb.get_setting.return_value = ''
    op.llm = SimpleNamespace(record_success=lambda *a: None, record_failure=lambda *a: None,
                             classify_error=lambda *a, **k: 'connection')
    op._get_provider_chain = lambda backend, model: list(chain)
    op._skipped_selection = lambda backend, head: None
    op._expand_slash_shortcut = lambda message: message
    op._build_chat_system_context = lambda tool_policy=None: 'You are CFOperator.'
    op.skills = {'k3s-health': {'name': 'k3s-health', 'description': 'Check the cluster',
                                'instructions': 'Look at nodes and pods.'}}
    op.containers = op.metrics = op.logs = op.alerts = None
    return op


def _done(op, message, monkeypatch, loaded_window=None):
    monkeypatch.setattr('requests.post', _TwoCalls())
    monkeypatch.setattr(CFOperator, '_ollama_loaded_context',
                        lambda self, url, model: loaded_window, raising=False)
    events = list(op.handle_chat_message_stream(message, [], actor_role='admin'))
    assert events[-1]['event'] == 'done', events[-1]
    return events[-1]['data']


def test_the_done_event_carries_what_the_provider_reported(monkeypatch):
    data = _done(_operator(), 'how are the pods?', monkeypatch, loaded_window=32768)
    usage = data['usage']
    assert usage['input_tokens'] == 250, "input is the sum over the loop's model calls"
    assert usage['output_tokens'] == 27
    assert data['tool_calls'] == 1
    assert isinstance(data['duration_s'], float) and data['duration_s'] >= 0


def test_the_context_figure_is_the_last_prompt_not_the_sum(monkeypatch):
    """Each call of the loop re-sends the conversation. Summed, a five-tool
    turn on a 10k prompt reads as 50k; what the window actually holds is the
    last call's prompt."""
    data = _done(_operator(), 'how are the pods?', monkeypatch, loaded_window=32768)
    assert data['usage']['last_prompt_tokens'] == 150
    assert data['usage']['last_prompt_tokens'] < data['usage']['input_tokens']


def test_a_skill_turn_reports_usage_too(monkeypatch):
    data = _done(_operator(), '/k3s-health', monkeypatch, loaded_window=32768)
    assert data['usage']['input_tokens'] == 250 and data['usage']['last_prompt_tokens'] == 150
    assert 'duration_s' in data


def test_the_window_is_the_runners_when_no_num_ctx_is_configured(monkeypatch):
    data = _done(_operator(), 'hi', monkeypatch, loaded_window=32768)
    assert data['usage']['context_window'] == 32768


def test_a_configured_num_ctx_is_the_window(monkeypatch):
    op = _operator(config={'chat': {'max_tool_iterations': 3}, 'llm': {'primary': {'num_ctx': 16384}}})
    data = _done(op, 'hi', monkeypatch, loaded_window=32768)
    assert data['usage']['context_window'] == 16384


def test_a_window_nobody_measured_is_not_reported(monkeypatch):
    """The history budget falls back to a default window, which is right for
    budgeting. Shown to an operator as "12k of 32k" it would be a denominator
    nobody measured, so the console gets none and draws no gauge."""
    data = _done(_operator(), 'hi', monkeypatch, loaded_window=None)
    assert 'context_window' not in data['usage']
    assert data['usage']['last_prompt_tokens'] == 150, "what is known is still reported"


def test_a_hosted_provider_has_no_window():
    op = _operator()
    op._ollama_num_ctx = lambda: 16384
    assert CFOperator._known_context_window(op, 'anthropic', None, 'claude') is None
    assert CFOperator._known_context_window(op, 'openrouter', 'https://x', 'm') is None
    assert CFOperator._known_context_window(op, 'ollama', 'http://up:11434', 'gemma4:26b') == 16384


def test_anthropic_prompt_size_counts_the_cached_part():
    """Its input_tokens is only what follows the last cache breakpoint; the
    context holds the cached part as well."""
    stats = _ToolLoopStats()
    stats.note_anthropic_usage({'input_tokens': 40, 'cache_read_input_tokens': 900,
                                'cache_creation_input_tokens': 60, 'output_tokens': 12})
    assert stats.last_prompt_tokens == 1000
    assert stats.result('x')['last_prompt_tokens'] == 1000


def test_a_call_that_reports_no_prompt_size_keeps_the_last_known():
    stats = _ToolLoopStats()
    stats.note_prompt(120)
    for nothing in (0, None, '130', True, -5, 12.5):
        stats.note_prompt(nothing)
    assert stats.last_prompt_tokens == 120


def test_usage_holds_only_numbers_that_were_measured():
    """Absent is not zero: the console has to tell "none" from "not known"."""
    assert _turn_usage({'input_tokens': 10, 'output_tokens': 0, 'last_prompt_tokens': None,
                        'context_window': True, 'response': 'secret text'}) == {'input_tokens': 10}
    assert _turn_usage(None) == {} and _turn_usage({}) == {}
    assert set(agent_module.TURN_USAGE_KEYS) == {'input_tokens', 'output_tokens',
                                                 'last_prompt_tokens', 'context_window'}


def test_the_payload_carries_counts_and_no_new_text(monkeypatch):
    data = _done(_operator(), 'how are the pods?', monkeypatch, loaded_window=32768)
    assert all(isinstance(v, int) for v in data['usage'].values())
