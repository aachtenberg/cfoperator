"""The model step of the dedupe funnel (CFOP-545): is this FIX the same
change as one of the open rows?

cfop-dedupe's tiers match what can be matched exactly: a NotReady node, a
target named the same way, a hard identifier, the key. They cannot match the
same change written twice in different words. #400, #401 and #402 named
plane-pgdb three ways for one ``ON DELETE CASCADE``, and since the funnel
closed the path leaks (CFOP-538) that is the class still reaching the queue.
A model can answer it, and this module asks.

It asks only when the tiers said new, outside ``_remediation_lock``, and only
for the paths no operator typed (investigation, sweep fallback, summary). The
answer is a hint. Under the lock the funnel decides as before, and a "new"
turns into a fold only when the hinted row is still open and the
``model_dedupe_fold`` flag lets it. Every failure means "no hint", so the
model can fold a row and never block one.

The seat is the ``dedupe`` scope (``LLM_SCOPES``), so it can be any backend
the chain knows (a local ollama, anthropic, groq, xai, gemini, deepseek,
openrouter), set from Admin → LLM. Nothing here names a host.
``benchmarks/dedupe_model_eval.py`` scores a seat against the operator's
duplicate notes through the same prompt and parser before the seat is used.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional

from prometheus_client import Counter, Histogram

logger = logging.getLogger("cfop.dedupe")

#: Rows the model is shown, newest first. The open set is a handful of rows;
#: this only bounds a backlog.
MAX_ROWS = 20
#: Characters of each recommendation the model reads. The essentials come
#: first in a FIX (CFOP-80), and twenty rows have to fit a small window.
REC_CHARS = 700
#: Longest reason kept: it is shown on the row and in the fold's reason.
REASON_CHARS = 300

SYSTEM_PROMPT = (
    "You decide whether a proposed infrastructure fix duplicates a fix that is "
    "already queued.\n\n"
    "Two fixes are the SAME when applying one makes the other unnecessary: the "
    "same change to the same thing, however differently it is worded and "
    "whatever names or kinds it uses for the target (a pod, its StatefulSet, "
    "the database inside it). Setting the same parameter to a different value "
    "(a zone of 256k or 1m, a memory limit of 512Mi or 1Gi) is still the same "
    "fix.\n\n"
    "They are DIFFERENT when they change different things, or make a different "
    "change to the same thing: raising a probe timeout and altering a table "
    "constraint on the same database are two fixes. A fix that only inspects or "
    "investigates is not the same as one that changes something.\n\n"
    "When in doubt, answer null. A missed duplicate costs one extra row; a wrong "
    "match hides a real fix.\n\n"
    'Reply with ONLY a JSON object: {"row": <the id of the queued row it '
    'duplicates, or null>, "reason": "<one sentence: what the two share, or why '
    'none matches>"}'
)

# mode: shadow (model_dedupe alone: the answer is recorded, nothing folds) |
# fold (model_dedupe_fold too). outcome: fold (it named a row) | none |
# malformed (no parseable answer, or a row it was not shown) | error (the
# call failed).
DEDUPE_MODEL = Counter(
    'cfoperator_dedupe_model_total',
    'Answers from the dedupe model step (CFOP-545)',
    ['mode', 'outcome'])
DEDUPE_MODEL_SECONDS = Histogram(
    'cfoperator_dedupe_model_seconds',
    'Dedupe model step calls, start to answer (CFOP-545)',
    ['outcome'], buckets=(0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 40.0, 80.0))


def _targets(payload: Dict[str, Any]) -> str:
    items = payload.get('targets') or payload.get('fold_targets') or []
    out = []
    for t in items if isinstance(items, list) else []:
        if isinstance(t, dict) and t.get('id'):
            repo = f" ({t['repo']})" if t.get('repo') else ''
            out.append(f"{t.get('kind') or '?'} {t['id']}{repo}")
    return '; '.join(out) or '-'


def _describe(fields: Dict[str, Any], host: Any, rclass: Any) -> List[str]:
    rec = ' '.join(str(fields.get('recommendation') or '').split())[:REC_CHARS]
    return [f"  class: {rclass or '-'}  host: {host or '-'}",
            f"  targets: {_targets(fields)}",
            f"  fix: {rec or '-'}"]


def build_messages(candidate: Dict[str, Any], rows: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """The one user message: the candidate (``dedupe_client.candidate_from``
    shape) and the rows (``list_open_remediations_for_dedupe`` shape, payload
    untrimmed), newest first."""
    lines = ["Proposed fix:",
             *_describe(candidate, candidate.get('host'), candidate.get('remediation_class')),
             "", "Queued rows:"]
    for r in rows[:MAX_ROWS]:
        payload = r.get('payload') if isinstance(r.get('payload'), dict) else {}
        host = (payload.get('target') or {}).get('host') if isinstance(payload.get('target'), dict) else None
        lines.append(f"#{r.get('id')} ({r.get('status')})")
        lines.extend(_describe(payload, host or r.get('host_id'), r.get('remediation_class')))
    lines += ["", "Which queued row, if any, is the same fix?"]
    return [{'role': 'user', 'content': '\n'.join(lines)}]


def parse_answer(text: str, shown: List[int]) -> Optional[Dict[str, Any]]:
    """``{'row_id', 'reason'}`` from the model's reply, or None when there is
    no answer to act on: no JSON object, or a row it was not shown. A row
    outside the set is a guess, not a match."""
    text = str(text or '')
    decoder = json.JSONDecoder()
    for m in re.finditer(r'\{', text):
        try:
            obj, _end = decoder.raw_decode(text, m.start())
        except ValueError:
            continue
        if not isinstance(obj, dict) or 'row' not in obj:
            continue
        row = obj.get('row')
        if isinstance(row, str):
            row = row.strip().lstrip('#')
            row = int(row) if row.isdigit() else (None if row.lower() in ('', 'null', 'none') else row)
        if row is not None and (isinstance(row, bool) or row not in shown):
            return None
        return {'row_id': row, 'reason': ' '.join(str(obj.get('reason') or '').split())[:REASON_CHARS]}
    return None


def ask(call: Callable[[List[Dict[str, str]], str], Dict[str, Any]],
        candidate: Dict[str, Any], rows: List[Dict[str, Any]], *, mode: str) -> Dict[str, Any]:
    """Ask the seat behind ``call`` about ``candidate``; never raises.

    ``call(messages, system)`` returns the chain's result dict (``response``,
    ``backend``, ``model``). The answer always carries ``outcome`` and
    ``mode``; ``row_id`` is set only when the outcome is ``fold``.
    """
    shown = rows[:MAX_ROWS]
    ids = [r.get('id') for r in shown]
    start = time.monotonic()
    answer: Dict[str, Any] = {'mode': mode, 'row_id': None, 'reason': '', 'backend': None, 'model': None}
    try:
        result = call(build_messages(candidate, shown), SYSTEM_PROMPT) or {}
    except Exception as e:
        logger.warning(f"Dedupe model call failed: {e}")
        answer['outcome'] = 'error'
    else:
        answer['backend'], answer['model'] = result.get('backend'), result.get('model')
        parsed = parse_answer(result.get('response', ''), ids)
        if parsed is None:
            logger.info(f"Dedupe model answer unusable: {str(result.get('response', ''))[:200]}")
            answer['outcome'] = 'malformed'
        else:
            answer.update(parsed)
            answer['outcome'] = 'fold' if parsed['row_id'] is not None else 'none'
            if parsed['row_id'] is not None:
                row = next(r for r in shown if r.get('id') == parsed['row_id'])
                payload = row.get('payload') if isinstance(row.get('payload'), dict) else {}
                answer['row_key'] = payload.get('dedupe_key')
    DEDUPE_MODEL.labels(mode=mode, outcome=answer['outcome']).inc()
    DEDUPE_MODEL_SECONDS.labels(outcome=answer['outcome']).observe(time.monotonic() - start)
    return answer
