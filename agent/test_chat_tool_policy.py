#!/usr/bin/env python3
"""The turn's ToolPolicy reaches the registry through the real chat loop (CFOP-124).

The registry decides what a policy allows (tools/test_tool_policy.py); these
prove the policy actually arrives there from the console route's entry point
— through the fallback chain, the provider loop and the tool dispatch — and
that a caller passing nothing gets the loop exactly as it was.
"""

import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator
from tools import ToolPolicy, ToolRegistry

TOOL_SCHEMA = {
    'type': 'function',
    'function': {
        'name': 'k8s_get_pods',
        'description': 'List pods',
        'parameters': {'type': 'object', 'properties': {}},
    },
}


class _Registry:
    """Records the policy each registry call received."""

    def __init__(self):
        self.schema_policies = []
        self.exec_policies = []

    def get_schemas(self, policy=None):
        self.schema_policies.append(policy)
        return [TOOL_SCHEMA]

    def execute(self, name, args, policy=None):
        self.exec_policies.append((name, policy))
        return {'pods': []}


class _LegacyRegistry:
    """The two-argument surface the rest of the suite stubs. Must keep working
    for every internal caller, which passes no policy."""

    def get_schemas(self):
        return [TOOL_SCHEMA]

    def execute(self, name, args):
        return {'pods': []}


def _operator(registry, max_iterations=4):
    op = CFOperator.__new__(CFOperator)
    op.config = {'chat': {'max_tool_iterations': max_iterations}}
    op.llm_timeout = 5
    op.tools = registry
    op.kb = SimpleNamespace(get_setting=lambda *a, **k: '')
    op.llm = SimpleNamespace(record_success=lambda *a: None,
                             record_failure=lambda *a: None,
                             classify_error=lambda *a, **k: 'connection')
    return op


def _ollama_msg(content='', tool_calls=None):
    message = {'role': 'assistant', 'content': content}
    if tool_calls:
        message['tool_calls'] = [{'function': {'name': n, 'arguments': {}}} for n in tool_calls]
    return message


class _FakePost:
    def __init__(self, scripts):
        self.scripts = {m: list(s) for m, s in scripts.items()}

    def __call__(self, url, json=None, headers=None, timeout=None):
        message = self.scripts[json['model']].pop(0)
        body = {'message': message, 'prompt_eval_count': 10, 'eval_count': 5}
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: body)


def _run(op, monkeypatch, **kwargs):
    monkeypatch.setattr('requests.post', _FakePost(
        {'m': [_ollama_msg(tool_calls=['k8s_get_pods']), _ollama_msg('done')]}))
    return op._chat_with_tools_inner(
        provider_type='ollama', url='http://fake:11434', model='m',
        messages=[{'role': 'user', 'content': 'check the pods'}],
        system_context='You are CFOperator.', **kwargs)


def test_the_policy_reaches_both_registry_calls(monkeypatch):
    reg = _Registry()
    policy = ToolPolicy(actor_role='member')
    out = _run(_operator(reg), monkeypatch, tool_policy=policy)
    assert out['response'] == 'done' and out['tool_calls'] == 1
    assert reg.schema_policies == [policy]
    assert reg.exec_policies == [('k8s_get_pods', policy)]


def test_no_policy_keeps_the_two_argument_surface(monkeypatch):
    out = _run(_operator(_LegacyRegistry()), monkeypatch)
    assert out['response'] == 'done' and out['tool_calls'] == 1


def test_the_fallback_chain_forwards_the_policy(monkeypatch):
    op = _operator(_Registry())
    seen = {}
    monkeypatch.setattr(op, '_get_provider_chain', lambda backend, model: [('ollama', 'http://x', 'm')])

    def inner(*args, **kwargs):
        seen.update(kwargs)
        return {'response': 'ok', 'tool_calls': 0}
    monkeypatch.setattr(op, '_chat_with_tools_inner', inner)
    policy = ToolPolicy(verify_only=True)
    op._chat_with_tools_with_fallback(messages=[], system_context='', tool_policy=policy)
    assert seen['tool_policy'] is policy


def test_the_stream_entry_builds_the_policy_from_role_and_mode(monkeypatch):
    op = _operator(_Registry())
    seen = []
    monkeypatch.setattr(op, '_expand_slash_shortcut', lambda m: m)
    monkeypatch.setattr(op, '_handle_chat_with_stream',
                        lambda *a, **kw: seen.append(kw.get('tool_policy')) or {'response': 'ok'})
    list(op.handle_chat_message_stream('hello', [], actor_role='member', verify_only=True))
    list(op.handle_chat_message_stream('hello', [], actor_role='admin'))
    list(op.handle_chat_message_stream('hello', []))
    assert seen == [ToolPolicy(actor_role='member', verify_only=True),
                    ToolPolicy(actor_role='admin', verify_only=False),
                    None]


def test_the_stream_entry_threads_the_policy_into_skills_too(monkeypatch):
    op = _operator(_Registry())
    seen = []
    monkeypatch.setattr(op, '_expand_slash_shortcut', lambda m: m)
    monkeypatch.setattr(op, '_execute_skill_stream',
                        lambda *a, **kw: seen.append(kw.get('tool_policy')) or {'response': 'ok'})
    list(op.handle_chat_message_stream('/some-skill x', [], actor_role='member'))
    assert seen == [ToolPolicy(actor_role='member')]


# --------------------------------------------------------------------------
# the system prompt tells the model what this turn is
# --------------------------------------------------------------------------

def _real_operator():
    reg_op = MagicMock()
    reg_op.config = {'infrastructure': {'hosts': {'box1': {'address': '10.9.8.7'}}}, 'search': {}}
    op = CFOperator.__new__(CFOperator)
    op.config = reg_op.config
    op.tools = ToolRegistry(reg_op)
    op.current_investigation = None
    op.last_sweep = time.time()
    op.kb = SimpleNamespace(find_learnings=lambda **k: [])
    return op


def test_a_member_prompt_lists_only_what_a_member_may_use():
    text = _real_operator()._build_chat_system_context(tool_policy=ToolPolicy(actor_role='member'))
    assert '- k8s_get_pods' in text
    assert '- ssh_execute' not in text and '- store_learning' not in text
    assert 'so an admin can do it' in text
    # The standing instruction to store learnings names a tool this turn lacks.
    assert 'ALWAYS use store_learning' not in text


def test_a_verification_prompt_says_so():
    text = _real_operator()._build_chat_system_context(
        tool_policy=ToolPolicy(actor_role='admin', verify_only=True))
    assert 'verification pass' in text
    assert '- ssh_execute' not in text and '- k8s_get_pods' in text


def test_an_internal_prompt_is_unchanged():
    text = _real_operator()._build_chat_system_context()
    assert '- ssh_execute' in text and 'ALWAYS use store_learning' in text
    assert 'verification pass' not in text and 'so an admin can do it' not in text
