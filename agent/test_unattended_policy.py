#!/usr/bin/env python3
"""Unattended runs observe; they do not change (CFOP-240).

Investigation 2558 ran ``rocm-smi --setfan 80`` as root on ubuntu-llm-01
through ssh_execute, because every internal tool loop ran with no policy and
the registry's gates only engage when one exists. In the 30 days before, the
same path restarted services and deployments seven times outside the queue.

Two guards. The first is about the class: every tool loop in the agent names
its policy, so the next internal caller cannot be unrestricted by omission.
The second drives ``_act`` itself against the real registry, so removing the
policy at the investigation's call site fails it.
"""

import ast
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator
from tools import UNATTENDED, ToolRegistry

AGENT_DIR = Path(__file__).resolve().parent
TOOL_LOOPS = {'_chat_with_tools', '_chat_with_tools_with_fallback', '_chat_with_tools_inner'}


def _tool_loop_calls():
    for path in sorted(AGENT_DIR.glob('*.py')):
        if path.name.startswith('test_'):
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in TOOL_LOOPS):
                yield f"{path.name}:{node.lineno}", node


def test_every_tool_loop_names_its_policy():
    """A hop forwards its own ``tool_policy``; everything else names one —
    UNATTENDED for a run with no person behind it. Omitting it, passing
    ``None``, or hiding it in ``**kwargs`` is how the investigation came to
    run unrestricted, so each fails here."""
    calls = list(_tool_loop_calls())
    # Not vacuous: the agent has well over ten of these.
    assert len(calls) >= 10, f"found only {len(calls)} tool-loop calls — is AGENT_DIR right?"
    unnamed = []
    for where, node in calls:
        keywords = {k.arg: k.value for k in node.keywords}
        value = keywords.get('tool_policy')
        if None in keywords:
            unnamed.append(f"{where} (policy hidden in **kwargs)")
        elif value is None:
            unnamed.append(f"{where} (no tool_policy)")
        elif isinstance(value, ast.Constant) and value.value is None:
            unnamed.append(f"{where} (tool_policy=None)")
    assert not unnamed, "tool loops that do not name their policy: " + ', '.join(unnamed)


# --------------------------------------------------------------------------
# the investigation, end to end through the real registry
# --------------------------------------------------------------------------

_RESPONSE = "Junction is hot.\nSTATUS: monitoring\nRECOMMENDATION: watch it"


def _investigating_operator(seen):
    """_act with its LLM loop replaced by one that does what 2558 did: try the
    fan write, then a read. Everything between _act and the tool is real."""
    registry_owner = MagicMock()
    registry_owner.config = {
        'infrastructure': {'hosts': {'ubuntu-llm-01': {'address': '10.0.0.9', 'user': 'ops'}}},
        'search': {},
    }
    registry = ToolRegistry(registry_owner)
    ran = seen.setdefault('ran', [])
    registry.tools['ssh_execute']['function'] = (
        lambda **kw: ran.append(kw['command']) or {'success': True, 'stdout': 'ok'})

    op = CFOperator.__new__(CFOperator)
    op.config = {}
    op.tools = registry
    op.kb = SimpleNamespace(
        start_investigation=lambda trigger: 2558,
        update_investigation=lambda **kw: True,
    )
    op._noise_config = lambda: {'enabled': False}
    op._verify_investigation_outcome = lambda outcome, alert, trigger: (outcome, '')
    op._maybe_propose_remediation = lambda *a, **k: None
    op._embed_investigation = lambda *a, **k: None

    def loop(**kw):
        policy = kw.get('tool_policy')
        seen['system_context'] = kw.get('system_context', '')
        seen['offered'] = {s['function']['name'] for s in registry.get_schemas(policy=policy)}
        seen['results'] = [
            registry.execute('ssh_execute', {'host': 'ubuntu-llm-01', 'command': command},
                             policy=policy)
            for command in ('rocm-smi --setfan 80', 'rocm-smi --showtemp')
        ]
        return {'backend': 'ollama', 'model': 'gemma4:26b', 'response': _RESPONSE, 'tool_calls': 2}

    op._chat_with_tools_with_fallback = loop
    return op


def _investigate(seen):
    op = _investigating_operator(seen)
    result = op._act({'trigger': 'GPU hotspot on headless-gpu > 100C for 10m', 'alert': {},
                      'known_learnings': [], 'similar_investigations': []})
    assert result['success'], result  # a swallowed exception must not pass as coverage
    return seen


def test_an_investigation_cannot_write_to_a_host_but_can_read_it():
    seen = _investigate({})
    refused, read = seen['results']
    assert refused.get('refused') is True, refused
    assert 'GPU settings' in refused['error'] and 'FIX' in refused['error']
    assert read == {'success': True, 'stdout': 'ok'}
    assert seen['ran'] == ['rocm-smi --showtemp'], "the fan write reached the host"


def test_an_investigation_is_offered_its_reads_and_gated_writes_only():
    offered = _investigate({})['offered']
    # Kept: the command-gated tools, and writes a person's gate stands behind.
    assert {'ssh_execute', 'k8s_exec_pod', 'store_learning'} <= offered
    # Withheld: changes that take effect the moment they return.
    assert not offered & {'ssh_restart_service', 'ssh_docker_restart', 'k8s_rollout_restart',
                          'resolve_remediation', 'triage_investigation',
                          'update_sweep_finding', 'queue_gitops_patch'}


def test_the_investigation_prompt_does_not_invite_a_fix():
    prompt = _investigate({})['system_context']
    assert 'read-only' in prompt
    assert 'you fixed it' not in prompt
    assert 'could not make yourself' not in prompt


def test_the_policy_is_the_shared_instance():
    """UNATTENDED is one instance so a call site names a mode, not flags."""
    assert UNATTENDED.unattended and not UNATTENDED.verify_only and UNATTENDED.actor_role is None
