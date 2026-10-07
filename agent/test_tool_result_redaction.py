"""Tool results are redacted at the one site that executes tools, so the
prompt, the transcript event and the memo cache all carry the same scrubbed
copy (CFOP-272).

Through the real tool loop with a scripted Ollama, not through the redactor
alone: the redactor's own tests live in tests/test_tool_result_redaction.py.
What this guards is the wiring — that the copy leaving the process is the
redacted one, on every provider branch, and that nothing executes a tool
anywhere else.
"""

import inspect
import json
import os
import re
import sys
from types import SimpleNamespace

from prometheus_client import REGISTRY

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator  # noqa: E402

SECRET = 'hunter2-very-secret'
TOKEN = 'cfop_bZqLYUXNabcdefghij'
TOOL_SCHEMA = {
    'type': 'function',
    'function': {'name': 'ssh_execute', 'description': 'Run a command',
                 'parameters': {'type': 'object', 'properties': {}}},
}


def _operator(redact=None):
    """A CFOperator with a stub tool that returns a secret in two shapes."""
    op = CFOperator.__new__(CFOperator)
    chat = {'max_tool_iterations': 4}
    if redact is not None:
        chat['redact_tool_results'] = redact
    op.config = {'chat': chat}
    op.llm_timeout = 5
    op.tools = SimpleNamespace(
        get_schemas=lambda: [TOOL_SCHEMA],
        execute=lambda name, args: {
            'stdout': f'POSTGRES_PASSWORD={SECRET}\nPATH=/usr/bin\n',
            'token': TOKEN, 'exit_code': 0, 'success': True,
        },
    )
    op.kb = SimpleNamespace(get_setting=lambda *a, **k: '')
    op.llm = SimpleNamespace(record_success=lambda *a: None, record_failure=lambda *a: None,
                             classify_error=lambda *a, **k: 'connection')
    return op


def _ollama_msg(content='', tool_calls=None):
    """One scripted Ollama assistant message."""
    message = {'role': 'assistant', 'content': content}
    if tool_calls:
        message['tool_calls'] = [{'function': {'name': n, 'arguments': {}}} for n in tool_calls]
    return message


class _FakePost:
    """Scripted Ollama /api/chat responses; records each request payload."""

    def __init__(self, messages):
        self.script = list(messages)
        self.payloads = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.payloads.append(json)
        body = {'message': self.script.pop(0), 'prompt_eval_count': 10, 'eval_count': 5}
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: body)


def _run(op, monkeypatch, events):
    """One tool-calling turn through the real loop; returns the fake transport."""
    fake = _FakePost([_ollama_msg(tool_calls=['ssh_execute']),
                      _ollama_msg(content='Fine.\nSTATUS: resolved\nRECOMMENDATION: No action needed')])
    monkeypatch.setattr('requests.post', fake)
    op._chat_with_tools_inner(
        provider_type='ollama', url='http://fake:11434', model='gemma4:26b',
        messages=[{'role': 'user', 'content': 'Investigate this alert: x'}],
        system_context='You are CFOperator.',
        event_callback=lambda kind, data: events.append((kind, data)))
    return fake


def _tool_messages(fake):
    """The role=tool messages across every request the loop sent."""
    return [m for p in fake.payloads for m in p['messages'] if m.get('role') == 'tool']


def test_the_prompt_carries_the_key_but_never_the_value(monkeypatch):
    """The Ollama payload carries POSTGRES_PASSWORD=*** and never the value."""
    fake = _run(_operator(), monkeypatch, [])
    sent = json.dumps(_tool_messages(fake))
    assert _tool_messages(fake), "the scripted turn must have produced a tool result"
    assert SECRET not in sent and TOKEN not in sent
    assert 'POSTGRES_PASSWORD=***' in sent and 'PATH=/usr/bin' in sent


def test_the_transcript_event_carries_the_same_redacted_copy(monkeypatch):
    """The tool_result event the console stores is the redacted copy."""
    events = []
    _run(_operator(), monkeypatch, events)
    results = json.dumps([d for k, d in events if k == 'tool_result'])
    assert results != '[]'
    assert SECRET not in results and TOKEN not in results and '***' in results


def test_the_knob_restores_raw_results(monkeypatch):
    """chat.redact_tool_results: false restores raw values everywhere."""
    events = []
    fake = _run(_operator(redact=False), monkeypatch, events)
    assert SECRET in json.dumps(_tool_messages(fake))
    assert SECRET in json.dumps([d for k, d in events if k == 'tool_result'])


def test_an_operator_built_without_config_still_redacts():
    """test_sweep_skill_memo builds a CFOperator with no config at all and
    calls _cached_tool_exec directly; the default must hold there too, not
    raise."""
    op = _operator()
    del op.config
    content, obj, _ = op._cached_tool_exec('ssh_execute', {}, {}, 6000)
    assert SECRET not in content and SECRET not in json.dumps(obj)


def test_redactions_are_counted_per_value_and_tool(monkeypatch):
    """The counter adds one per value replaced, labelled by tool."""
    before = REGISTRY.get_sample_value('cfoperator_tool_result_redactions_total',
                                       {'tool_name': 'ssh_execute'}) or 0.0
    _run(_operator(), monkeypatch, [])
    after = REGISTRY.get_sample_value('cfoperator_tool_result_redactions_total',
                                      {'tool_name': 'ssh_execute'})
    # POSTGRES_PASSWORD=… in stdout plus the `token` key: two values.
    assert after - before == 2.0


def test_the_memo_cache_holds_the_redacted_copy():
    """A cache hit hands back the redacted copy, never the raw result."""
    op = _operator()
    cache = {}
    first, obj, cached = op._cached_tool_exec('ssh_execute', {}, cache, 6000)
    op._MEMOIZABLE_TOOLS = frozenset({'ssh_execute'}) | op._MEMOIZABLE_TOOLS
    _, obj2, _ = op._cached_tool_exec('ssh_execute', {}, cache, 6000)
    assert SECRET not in first and SECRET not in json.dumps(obj)
    assert SECRET not in json.dumps(list(cache.values())), "a cache hit hands back whatever was cached"


def test_every_provider_branch_appends_only_what_dispatch_handed_back():
    """The class of regression: a new provider branch that builds its tool
    message from a fresh execute, or from anything but the `content` that
    _dispatch_tool_call returned, bypasses the redaction."""
    loop = inspect.getsource(CFOperator._chat_with_tools_inner)
    appends = re.findall(r"'role': 'tool'[\s\S]*?\}\)", loop)
    assert len(appends) >= 3, "expected a tool-role append per provider branch"
    for append in appends:
        assert ("'content': content" in append
                or "'content': json.dumps([tr['content'] for tr in tool_results])" in append), append
    assert loop.count('self._dispatch_tool_call(') >= 3
    for fn in (CFOperator._chat_with_tools_inner, CFOperator._dispatch_tool_call):
        assert 'self.tools.execute(' not in inspect.getsource(fn), \
            f"{fn.__name__} executes a tool itself; only _cached_tool_exec may"
    assert 'self._redact_result(' in inspect.getsource(CFOperator._cached_tool_exec)
