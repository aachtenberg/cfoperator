#!/usr/bin/env python3
"""What an investigation is shown from the KB (CFOP-377).

The rows used here are the real ones from remediation #275: learning 2459 is
the auto-correlation the investigation restated almost word for word, 3785 is
the operator correction that ranked below it, 1338 is the unrelated row that
filled the third slot at 0.599. The orient test drives ``_orient`` itself, so
a change that filters in ``select_prompt_learnings`` but stops calling it --
or goes back to asking the KB for only three -- fails here.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator
from agent.agent import _past_context_blocks
from past_learnings import (
    CANDIDATE_LIMIT, PROMPT_LIMIT, SIMILARITY_FLOOR, provenance,
    select_prompt_learnings,
)

AUTO_2459 = {'id': 2459, 'learning_type': 'root_cause', 'vector_similarity': 0.822,
             'fts_rank': 0, 'tags': ['correlation', 'automated'],
             'title': 'RabbitMQ memory watermark causes API unresponsiveness',
             'description': 'High memory usage on the RabbitMQ pod triggers a high watermark alarm'}
OPERATOR_3785 = {'id': 3785, 'learning_type': 'antipattern', 'vector_similarity': 0.784,
                 'fts_rank': 0.059,
                 'tags': ['rabbitmq', 'memsup', 'false-positive', 'source:operator', 'confidence:1.00'],
                 'title': 'system_memory_high_watermark is OTP memsup noise',
                 'description': 'memsup counts page cache; rabbit_alarm:get_alarms() is []'}
WEAK_1338 = {'id': 1338, 'learning_type': 'pattern', 'vector_similarity': 0.599,
             'fts_rank': 0, 'tags': ['ollama', 'memory'],
             'title': 'Long-running Ollama service with high memory consumption',
             'description': 'potential memory leak'}


def _ids(rows):
    return [r['id'] for r in rows]


def test_auto_correlations_and_weak_matches_are_dropped_and_counted():
    kept, dropped = select_prompt_learnings([AUTO_2459, OPERATOR_3785, WEAK_1338])
    assert _ids(kept) == [3785]
    assert dropped == {'auto_correlation': 1, 'under_floor': 1}


def test_floor_is_inclusive_and_sits_between_the_measured_bands():
    # Relevant hits measured 0.70-0.88, unrelated 0.59-0.65.
    assert 0.65 <= SIMILARITY_FLOOR < 0.70
    at = dict(WEAK_1338, id=1, vector_similarity=SIMILARITY_FLOOR)
    below = dict(WEAK_1338, id=2, vector_similarity=SIMILARITY_FLOOR - 0.001)
    assert _ids(select_prompt_learnings([at, below])[0]) == [1]


def test_a_full_text_match_is_kept_without_a_vector_score():
    # Hybrid search reports vector_similarity 0 for a learning whose embedding
    # the sweep has not written yet; it only came back because FTS matched.
    unembedded = dict(OPERATOR_3785, id=3784, vector_similarity=0, fts_rank=0.013)
    unmatched = dict(OPERATOR_3785, id=9, vector_similarity=0, fts_rank=0)
    assert _ids(select_prompt_learnings([unembedded, unmatched])[0]) == [3784]


def test_fts_only_rows_carry_no_score_and_pass():
    # find_learnings (embeddings down) has no vector_similarity key at all.
    fts_only = {k: v for k, v in WEAK_1338.items() if k not in ('vector_similarity', 'fts_rank')}
    assert _ids(select_prompt_learnings([fts_only])[0]) == [1338]


def test_auto_correlations_are_held_back_even_when_nothing_else_survives():
    # #3296's shape: every candidate an auto-correlation at ~0.8. Showing them
    # labelled "unverified" was the rejected alternative; none is shown.
    autos = [dict(AUTO_2459, id=i, vector_similarity=0.8) for i in range(5)]
    kept, dropped = select_prompt_learnings(autos)
    assert kept == [] and dropped['auto_correlation'] == 5


def test_keeps_the_kb_order_and_stops_at_the_limit():
    rows = [dict(OPERATOR_3785, id=i, vector_similarity=0.9 - i / 100) for i in range(6)]
    kept, _ = select_prompt_learnings(rows)
    assert _ids(kept) == list(range(PROMPT_LIMIT))


def test_provenance_reads_each_writers_tags():
    assert provenance(OPERATOR_3785) == 'from operator'
    assert provenance({'tags': ['cfop-reverify', 'remediation:275']}) == 'from a rejected remediation'
    assert provenance({'tags': ['plane', 'rabbitmq']}) == 'from a past investigation'
    assert provenance({'tags': None}) == 'from a past investigation'


def _orienting_operator(candidates, asked):
    op = CFOperator.__new__(CFOperator)
    op.embeddings = SimpleNamespace(is_available=lambda: True,
                                    generate_embedding=lambda text: [0.1, 0.2])

    def hybrid(query_text, query_embedding, limit):
        asked.append(limit)
        return candidates[:limit]

    op.kb = SimpleNamespace(_kb=SimpleNamespace(
        find_learnings_hybrid=hybrid,
        find_similar_investigations_hybrid=lambda **kw: [],
    ))
    return op


def test_orient_asks_for_candidates_and_shows_only_what_survives():
    asked = []
    op = _orienting_operator([AUTO_2459, OPERATOR_3785, WEAK_1338], asked)
    context = op._orient({'trigger': 'plane-rabbitmq-wl-0 is experiencing unexpected AMQP '
                                     'and TCP connection closures from clients.'})
    assert asked == [CANDIDATE_LIMIT] and CANDIDATE_LIMIT > PROMPT_LIMIT
    assert _ids(context['known_learnings']) == [3785]


def test_prompt_lines_say_where_each_came_from_and_the_caution_is_outside_the_frame():
    from prompt_injection import DATA_END
    block, _ = _past_context_blocks({'known_learnings': [OPERATOR_3785]})
    assert '[antipattern · from operator] system_memory_high_watermark' in block
    framed, after = block.rsplit(DATA_END, 1)
    assert 'not facts' not in framed
    assert 'not facts about this alert' in after
