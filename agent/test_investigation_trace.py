"""The investigation trace behind the console's Explain view (CFOP-324).

Through the real tool loop with a scripted Ollama, like the CFOP-272 wiring
tests: what is guarded is what lands in ``investigation_events`` — one row per
tool call holding the redacted string the model was handed, what the model
was told and said, provider switches, the answer — and that the recorder can
never take an investigation down with it.
"""

import inspect
import json
import os
import re
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator  # noqa: E402
import investigation_trace  # noqa: E402
from investigation_trace import InvestigationTrace, MAX_ROWS  # noqa: E402

SECRET = 'hunter2-very-secret'
TOOL_SCHEMA = {
    'type': 'function',
    'function': {'name': 'ssh_execute', 'description': 'Run a command',
                 'parameters': {'type': 'object', 'properties': {}}},
}


class FakeKB:
    """Collects the rows a trace writes; ``fail`` makes every write raise."""

    def __init__(self, fail=False):
        self.rows = []
        self.fail = fail

    def record_investigation_event(self, **row):
        if self.fail:
            raise RuntimeError('database is gone')
        self.rows.append(row)
        return len(self.rows)

    def get_setting(self, *a, **k):
        return ''


def _operator(kb, result=None):
    op = CFOperator.__new__(CFOperator)
    op.config = {'chat': {'max_tool_iterations': 4}}
    op.llm_timeout = 5
    op.tools = SimpleNamespace(
        get_schemas=lambda: [TOOL_SCHEMA],
        execute=lambda name, args: result or {
            'stdout': f'POSTGRES_PASSWORD={SECRET}\nBOOT_TIMEOUT_MS=60000\n',
            'exit_code': 0, 'success': True},
    )
    op.kb = kb
    op.llm = SimpleNamespace(record_success=lambda *a: None, record_failure=lambda *a: None,
                             classify_error=lambda *a, **k: 'connection')
    return op


class _FakePost:
    def __init__(self, messages):
        self.script = list(messages)

    def __call__(self, url, json=None, headers=None, timeout=None):
        body = {'message': self.script.pop(0), 'prompt_eval_count': 10, 'eval_count': 5}
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: body)


def _msg(content='', tools=()):
    m = {'role': 'assistant', 'content': content}
    if tools:
        m['tool_calls'] = [{'function': {'name': n, 'arguments': {'command': f'cat /etc/{n}.env',
                                                                    'password': SECRET}}}
                           for n in tools]
    return m


def _investigate(monkeypatch, kb, script=None):
    """One investigation-shaped turn through the real loop, traced."""
    op = _operator(kb)
    script = script or [_msg('Reading the runner env first.', ['ssh_execute']),
                        _msg('BOOT_TIMEOUT_MS is low.\nSTATUS: needs_action\nRECOMMENDATION: raise it')]
    monkeypatch.setattr('requests.post', _FakePost(script))
    trace = InvestigationTrace(kb, 2871)
    trace.prompt('You are CFOperator. Learning #412: DB_PASSWORD=from-a-learning',
                 'Investigate this alert: KubeJobFailed')
    result = op._chat_with_tools_inner(
        provider_type='ollama', url='http://fake:11434', model='gemma4:26b',
        messages=[{'role': 'user', 'content': 'Investigate this alert: KubeJobFailed'}],
        system_context='You are CFOperator.', event_callback=trace)
    result.setdefault('backend', 'ollama')
    result.setdefault('model', 'gemma4:26b')
    trace.answer(result)
    return kb.rows


def _kinds(rows):
    return [r['event_type'] if r['event_type'] == 'tool_call' else r.get('action_type') for r in rows]


def test_a_traced_investigation_records_every_step_in_order(monkeypatch):
    rows = _investigate(monkeypatch, FakeKB())
    assert _kinds(rows) == ['prompt', 'assistant', 'tool_call', 'answer']
    tool = rows[2]
    assert tool['tool_name'] == 'ssh_execute'
    assert tool['tool_output']['status'] == 'success'
    assert isinstance(tool['duration_ms'], int)
    assert 'BOOT_TIMEOUT_MS=60000' in tool['reasoning_text'], \
        "the row keeps the string the model was handed, not a 500-char preview"
    assert rows[1]['reasoning_text'] == 'Reading the runner env first.'
    assert rows[1]['action_target'] == 'ollama/gemma4:26b'
    assert rows[3]['action_target'] == 'ollama/gemma4:26b'


def test_no_secret_reaches_a_stored_row(monkeypatch):
    """The acceptance criterion: a secret in a tool result never lands in the
    stored trace. Nor does one in the arguments, or one a learning carried
    into the prompt.

    Mutation check: drop redact_tool_result from _cached_tool_exec, or the
    _redacted_text call from prompt(), and this goes red.
    """
    rows = _investigate(monkeypatch, FakeKB())
    stored = json.dumps(rows, default=str)
    assert SECRET not in stored and 'from-a-learning' not in stored
    assert 'POSTGRES_PASSWORD=***' in stored and 'DB_PASSWORD=***' in stored


def test_a_refused_call_is_recorded_as_refused_not_as_an_error(monkeypatch):
    kb = FakeKB()
    op = _operator(kb, result={'refused': 'not run: unattended investigations may not change host state'})
    monkeypatch.setattr('requests.post', _FakePost([
        _msg('', ['ssh_execute']), _msg('done\nSTATUS: monitoring\nRECOMMENDATION: none')]))
    op._chat_with_tools_inner(provider_type='ollama', url='http://fake', model='m',
                              messages=[{'role': 'user', 'content': 'x'}], system_context='s',
                              event_callback=InvestigationTrace(kb, 5))
    tool = [r for r in kb.rows if r['event_type'] == 'tool_call'][0]
    assert tool['tool_output']['status'] == 'refused' and tool['success'] is False


def test_the_chat_stream_keeps_its_preview_size(monkeypatch):
    """Only a callback that asks gets the full string: the console chat polls
    these events into the browser, and its preview is 500 chars on purpose."""
    events = []
    op = _operator(FakeKB(), result={'stdout': 'x' * 5000})
    monkeypatch.setattr('requests.post', _FakePost([
        _msg('', ['ssh_execute']), _msg('ok')]))
    op._chat_with_tools_inner(provider_type='ollama', url='http://fake', model='m',
                              messages=[{'role': 'user', 'content': 'x'}], system_context='s',
                              event_callback=lambda kind, data: events.append((kind, data)))
    result = [d for k, d in events if k == 'tool_result'][0]
    assert 'content' not in result and len(result['result']) <= 500


def test_the_cap_writes_one_truncation_row_and_stops():
    kb = FakeKB()
    trace = InvestigationTrace(kb, 9)
    for i in range(MAX_ROWS + 50):
        trace('tool_call', {'tool': 't', 'args': {'i': i}})
        trace('tool_result', {'tool': 't', 'content': 'ok', 'status': 'success'})
    assert len(kb.rows) == MAX_ROWS
    assert kb.rows[-1]['action_type'] == 'truncated'
    assert sum(1 for r in kb.rows if r.get('action_type') == 'truncated') == 1


def test_a_failing_knowledge_base_never_reaches_the_investigation(monkeypatch):
    """The trace is evidence about the investigation, not part of it."""
    seen = []
    kb = FakeKB(fail=True)
    op = _operator(kb)
    monkeypatch.setattr('requests.post', _FakePost([
        _msg('looking', ['ssh_execute']), _msg('answer\nSTATUS: resolved\nRECOMMENDATION: none')]))
    trace = InvestigationTrace(kb, 3, on_row=lambda kind, res: seen.append((kind, res)))
    trace.prompt('s', 'u')
    result = op._chat_with_tools_inner(provider_type='ollama', url='http://fake', model='m',
                                       messages=[{'role': 'user', 'content': 'x'}],
                                       system_context='s', event_callback=trace)
    trace.answer(result)
    assert result['response'].startswith('answer')
    assert seen and all(res == 'failed' for _, res in seen)


def test_an_offline_placeholder_id_records_nothing():
    kb = FakeKB()
    trace = InvestigationTrace(kb, -4)
    trace.prompt('s', 'u')
    trace('fallback', {'from': 'a', 'to': 'b'})
    assert kb.rows == []


def test_a_provider_switch_is_a_row():
    kb = FakeKB()
    InvestigationTrace(kb, 1)('fallback', {'from': 'ollama/gemma4:26b', 'to': 'groq/llama',
                                          'reason': 'timeout'})
    assert kb.rows[0]['action_type'] == 'fallback'
    assert kb.rows[0]['action_target'] == 'groq/llama'
    assert 'ollama/gemma4:26b → groq/llama' in kb.rows[0]['reasoning_text']


# -- structural guards -------------------------------------------------------

def test_every_provider_branch_streams_the_text_beside_its_tool_calls():
    """A provider branch that runs tools without emitting the model's text
    leaves a gap in every trace that took it. Counted against the branches
    that dispatch tools, so a new provider cannot quietly skip it.

    Mutation check: delete one _emit_assistant_text call and this goes red.
    """
    src = inspect.getsource(CFOperator._chat_with_tools_inner)
    tool_branches = len(re.findall(r'^\s+if (tool_calls|tool_use_blocks):\s*$', src, re.M))
    emits = src.count('self._emit_assistant_text(')
    assert tool_branches >= 3
    assert emits == tool_branches, (
        f"{tool_branches} branches run tools, {emits} emit the model's text")


def test_the_investigation_loop_is_traced():
    """_act runs the investigation's tool loop with the recorder on it.

    Mutation check: drop event_callback=trace from _act and this goes red.
    """
    src = inspect.getsource(CFOperator._act)
    call = src[src.index('self._chat_with_tools_with_fallback('):]
    call = call[:call.index(')\n')]
    assert 'event_callback=trace' in call
    assert 'InvestigationTrace(' in src and 'trace.prompt(' in src and 'trace.answer(' in src


def test_the_recorder_writes_only_kinds_the_console_reads():
    """The drawer renders by kind; a kind it does not know shows as bare text."""
    console = open(os.path.join(os.path.dirname(__file__), '..', 'ui', 'investigations.html'),
                   encoding='utf-8').read()
    for kind in investigation_trace.REASONING_KINDS:
        assert f"s.kind==='{kind}'" in console, f"the drawer has no rendering for {kind!r} rows"
