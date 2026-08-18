#!/usr/bin/env python3
"""The memory-beat seam (CFOP-31): orient-phase similar-investigation hits
must be *persisted* into findings['similar_past'], not just injected into the
prompt. Whether the LLM mentions a past investigation in its prose is up to
the model; whether this run was informed by one is a fact about the run, and
the kind demo (and the console drawer) assert on the recorded fact.

These drive _act() itself with a scripted LLM result and capture what lands
in kb.update_investigation — removing the persistence line in _act fails
them; a change that only touches the prompt block does not save it.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator

_RESPONSE = "Pod is flapping.\nSTATUS: monitoring\nRECOMMENDATION: watch it"


def _operator(captured):
    op = CFOperator.__new__(CFOperator)
    op.config = {}
    op.kb = SimpleNamespace(
        start_investigation=lambda trigger: 77,
        update_investigation=lambda **kw: captured.update(kw) or True,
    )
    op._noise_config = lambda: {'enabled': False}
    op._chat_with_tools_with_fallback = lambda **kw: {
        'backend': 'ollama', 'model': 'demo', 'response': _RESPONSE, 'tool_calls': 2,
    }
    op._verify_investigation_outcome = lambda outcome, alert, trigger: (outcome, '')
    op._maybe_propose_remediation = lambda *a, **k: None
    op._embed_investigation = lambda *a, **k: None
    return op


def _hybrid_hit(i, trigger="Pod demo/boom is in CrashLoopBackOff"):
    """Shape returned by find_similar_investigations_hybrid."""
    return {
        'id': i, 'trigger': trigger, 'outcome': 'needs_action',
        'vector_similarity': 0.81, 'fts_rank': 0.4, 'combined_score': 0.71,
        'findings': {'response': 'noise ' * 500}, 'embedding_text': 'x' * 4000,
    }


def _run(op, similar):
    context = {'trigger': 'CrashLoopBackOff in demo/boom', 'alert': {},
               'known_learnings': [], 'similar_investigations': similar}
    result = op._act(context)
    assert result['success'], result  # a swallowed exception must not pass as coverage
    return result


def test_similar_past_is_persisted_into_findings():
    captured = {}
    op = _operator(captured)
    _run(op, [_hybrid_hit(41)])
    cited = captured['findings']['similar_past']
    assert cited == [{'id': 41,
                      'trigger': 'Pod demo/boom is in CrashLoopBackOff',
                      'outcome': 'needs_action',
                      'similarity': 0.71}]


def test_no_similars_means_no_key():
    captured = {}
    op = _operator(captured)
    _run(op, [])
    assert 'similar_past' not in captured['findings']


def test_citations_are_trimmed_not_dumped():
    """Cap at 3 hits and 200 chars of trigger — findings is a rendered JSON
    payload, not a dump of embeddings-table rows (which carry findings blobs
    and embedding_text)."""
    captured = {}
    op = _operator(captured)
    _run(op, [_hybrid_hit(i, trigger='t' * 999) for i in range(6)])
    cited = captured['findings']['similar_past']
    assert [c['id'] for c in cited] == [0, 1, 2]
    assert all(len(c['trigger']) == 200 for c in cited)
    assert all(set(c) == {'id', 'trigger', 'outcome', 'similarity'} for c in cited)


def test_vector_only_shape_also_cites():
    """The vector-only fallback path returns 'similarity' instead of
    'combined_score'; both shapes must survive persistence."""
    captured = {}
    op = _operator(captured)
    _run(op, [{'id': 9, 'trigger': 'x', 'outcome': 'resolved', 'similarity': 0.9}])
    assert captured['findings']['similar_past'][0]['similarity'] == 0.9
