#!/usr/bin/env python3
"""
Score a dedupe-model seat (CFOP-545) against the operator's own duplicate notes.

The question the model step answers is "is this new FIX the same change as
one of these open rows?", and the queue already holds the answers: every row
the operator closed as "Duplicate of #N" is a labelled positive, and every
row nobody marked is a labelled negative. This harness rebuilds each row's
moment of arrival (the rows open when it was created) and asks a seat what
the agent would have asked it then.

Fidelity to production (``CFOperator._dedupe_model_hint``):
  - the prompt, the parser and the outcome are ``dedupe_model.ask`` itself,
    so the system prompt, the message, the row cap and the "a row it was not
    shown is not a match" rule are the ones the agent runs;
  - the request body per backend is the tool loop's one-shot body: ollama
    through ``CFOperator._ollama_chat_payload``, OpenAI-compatible backends
    with the loop's temperature, max_tokens and registry params
    (``_openai_compat_tool_loop_params``), Anthropic with no sampling
    parameters; keys come from ``llm_keys`` (env, since there is no DB here);
  - NOT mirrored: on an OpenAI-compatible seat the agent's one-shot also
    offers the unattended tool schemas and the final-answer nudge. The
    harness sends neither, so a model that would rather call a tool than
    answer scores better here than it will live. Shadow mode is the check
    for that.

Labels:
  - positives: rows linked by a note (``Duplicate of #N``, ``Duplicates #A
    and #B``, ``one of 13 duplicate rows (#334-#347)``, ``duplicate of
    CFOP-304``) plus ``KNOWN`` below. A positive case expects any open row in
    its cluster; when none was open any more it is scored as a negative,
    since the model is only shown open rows.
  - negatives: unmarked rows that arrived with rows open, hardest first (a
    shared host, a shared target, overlapping words), capped by
    ``--negatives``. An unmarked duplicate the operator never noted shows up
    as a false fold, and the first run (2026-10-28) found that most of them
    are exactly that: the notes mark perhaps half the queue's duplicates. So
    every fold no note covers is printed in full, a human gives it a verdict
    in ``dedupe_model_adjudication.json`` (S same fix, B borderline, D a
    different fix), and only D counts as a wrong fold. ``--replay`` re-grades
    a saved run against the file without calling anything.
  - a "duplicate of CFOP-N" note groups rows closed against one issue, which
    can be two fixes for one problem (#220 configures NetworkManager, #222
    changes a DaemonSet's dnsPolicy, both closed by one PR). Every seat
    misses that pair, and it is the label that is loose, not the model.

Usage:
    # The queue, from a pod with a scoped token:
    kubectl -n apps exec deploy/cfoperator-mcp -- sh -c \\
      'curl -s -H "Authorization: Bearer $CFOP_API_TOKEN" "$CFOP_AGENT_URL/api/remediations?limit=1000"' \\
      > /tmp/rows.json

    # The cases, nothing called:
    PYTHONPATH=agent:. .venv/bin/python benchmarks/dedupe_model_eval.py --rows /tmp/rows.json --cases-only

    # Seats are backend:model, and an ollama seat names its host after '@':
    PYTHONPATH=agent:. .venv/bin/python benchmarks/dedupe_model_eval.py --rows /tmp/rows.json \\
        --seat ollama:gemma4:26b@http://192.168.0.150:11434 --seat xai:grok-4.5 \\
        --seat anthropic:claude-haiku-5-5 --adjudication benchmarks/dedupe_model_adjudication.json \\
        --output benchmarks/dedupe_model_eval_2026-10-28.json --no-reasons

    # Re-grade a saved run after adding verdicts, nothing called:
    PYTHONPATH=agent:. .venv/bin/python benchmarks/dedupe_model_eval.py --rows /tmp/rows.json \\
        --adjudication benchmarks/dedupe_model_adjudication.json --replay benchmarks/<run>.json
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

import dedupe_model
import llm_keys
from agent import CFOperator, OPENAI_COMPAT_PROVIDERS

#: Duplicates the operator closed somewhere other than the row's own note.
#: #401 and #402 were cancelled in Plane as duplicates of CFOP-540 (#400) on
#: 2026-10-28; the tracker's rejection carries no "duplicate of".
KNOWN = {401: 400, 402: 400}

_DUP_LIST = re.compile(r'[Dd]uplicates?\s+(?:of\s+)?(?:remediation\s+)?((?:#\d+(?:\s*(?:,\s*and|,|and)\s*)?)+)')
_DUP_RANGE = re.compile(r'#(\d+)\s*[-–]\s*#(\d+)')
_DUP_ISSUE = re.compile(r'[Dd]uplicate\s+of\s+(CFOP-\d+)')
_WORD = re.compile(r'[a-z0-9_./-]{4,}')


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

def _notes(row: Dict[str, Any]) -> str:
    return f"{json.dumps(row.get('result'), default=str)} {row.get('last_error') or ''}"


def links(rows: List[Dict[str, Any]]) -> List[Tuple[Any, Any]]:
    """(a, b) pairs the notes say are one fix. A node is a row id, or a
    tracker key a group of rows was closed against."""
    ids = {r['id'] for r in rows}
    out = [(a, b) for a, b in KNOWN.items() if a in ids and b in ids]
    for r in rows:
        text = _notes(r)
        for m in _DUP_LIST.finditer(text):
            out += [(r['id'], int(n)) for n in re.findall(r'#(\d+)', m.group(1)) if int(n) in ids]
        if re.search(r'duplicate', text, re.I):
            # "one of 13 duplicate rows (#334-#347)": the row joins the
            # range's first, not every id in the range (#341 is not one).
            out += [(r['id'], int(m.group(1))) for m in _DUP_RANGE.finditer(text) if int(m.group(1)) in ids]
        out += [(r['id'], m.group(1)) for m in _DUP_ISSUE.finditer(text)]
    return out


def clusters(rows: List[Dict[str, Any]]) -> Dict[Any, Any]:
    """Union-find over ``links``: node → its cluster's root."""
    parent: Dict[Any, Any] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b in links(rows):
        parent[find(a)] = find(b)
    return {x: find(x) for x in list(parent)}


def open_at(rows: List[Dict[str, Any]], row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The rows open when ``row`` arrived, newest first: created before it and
    not closed (completed_at) until after it."""
    t = row['created_at']
    return sorted((x for x in rows if x['created_at'] < t and (not x.get('completed_at') or x['completed_at'] > t)),
                  key=lambda x: x['created_at'], reverse=True)


def candidate_of(row: Dict[str, Any]) -> Dict[str, Any]:
    """The row as the enqueue that made it would have asked: the
    ``dedupe_client.candidate_from`` fields."""
    p = row.get('payload') or {}
    host = (p.get('target') or {}).get('host') if isinstance(p.get('target'), dict) else None
    cand = {'dedupe_key': p.get('dedupe_key'), 'recommendation': p.get('recommendation'),
            'targets': p.get('targets'), 'fold_targets': p.get('fold_targets'),
            'host': host or (row.get('host_id') if row.get('host_id') != 'default' else None),
            'title': p.get('title'), 'remediation_class': row.get('remediation_class'),
            'source': p.get('source')}
    return {k: v for k, v in cand.items() if v is not None}


def shown_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """An open row as the agent's snapshot holds it. Its status then is not
    recorded, and its status now would leak how it ended, so: 'open'."""
    return {'id': row['id'], 'status': 'open', 'remediation_class': row.get('remediation_class'),
            'host_id': row.get('host_id'), 'payload': row.get('payload') or {}}


def _overlap(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    """How alike two rows look to a string match: the hard negatives first."""
    pa, pb = a.get('payload') or {}, b.get('payload') or {}
    wa = set(_WORD.findall(str(pa.get('recommendation') or '').lower()))
    wb = set(_WORD.findall(str(pb.get('recommendation') or '').lower()))
    words = len(wa & wb) / max(1, len(wa | wb))
    host = 1.0 if a.get('host_id') not in (None, 'default') and a.get('host_id') == b.get('host_id') else 0.0
    ta = {str(t.get('id')) for t in (pa.get('targets') or pa.get('fold_targets') or []) if isinstance(t, dict)}
    tb = {str(t.get('id')) for t in (pb.get('targets') or pb.get('fold_targets') or []) if isinstance(t, dict)}
    return words + host + (1.0 if ta & tb else 0.0)


def build_cases(rows: List[Dict[str, Any]], negatives: int) -> List[Dict[str, Any]]:
    rows = sorted(rows, key=lambda r: r['created_at'])
    root = clusters(rows)
    dups, distinct = [], []
    for r in rows:
        shown = open_at(rows, r)[:dedupe_model.MAX_ROWS]
        if not shown:
            continue
        mine = root.get(r['id'])
        expected = sorted(x['id'] for x in shown if mine is not None and root.get(x['id']) == mine)
        case = {'id': r['id'], 'kind': 'duplicate' if expected else 'distinct', 'expected': expected,
                'candidate': candidate_of(r), 'rows': [shown_row(x) for x in shown],
                'hardness': round(max(_overlap(r, x) for x in shown), 3),
                'cluster_open_gone': bool(mine is not None and not expected)}
        (dups if expected else distinct).append(case)
    distinct.sort(key=lambda c: c['hardness'], reverse=True)
    return dups + distinct[:negatives]


# ---------------------------------------------------------------------------
# Seats
# ---------------------------------------------------------------------------

class _Seatless:
    """What the production body builders read off ``self``: no knowledge
    base (keys come from env), and no num_ctx unless --num-ctx says so."""
    kb = None

    def __init__(self, num_ctx: Optional[int] = None):
        self._num_ctx = num_ctx

    def _ollama_num_ctx(self):
        return self._num_ctx

    def provider_api_key(self, backend):
        return llm_keys.api_key(None, backend)


def parse_seat(spec: str) -> Dict[str, str]:
    """``backend:model`` or ``ollama:model@url``. Models may contain ':'."""
    backend, _, rest = spec.partition(':')
    model, _, url = rest.partition('@')
    if not backend or not model:
        raise SystemExit(f"--seat {spec!r}: want backend:model")
    if backend == 'ollama' and not url:
        raise SystemExit(f"--seat {spec!r}: an ollama seat names its host, ollama:model@http://host:11434")
    if backend not in ('ollama', 'anthropic') and backend not in OPENAI_COMPAT_PROVIDERS:
        raise SystemExit(f"--seat {spec!r}: unknown backend; known: ollama, anthropic, "
                         f"{', '.join(OPENAI_COMPAT_PROVIDERS)}")
    return {'backend': backend, 'model': model, 'url': url.rstrip('/'), 'name': spec}


def caller(seat: Dict[str, str], timeout: float, num_ctx: Optional[int] = None):
    """``call(messages, system)`` for ``dedupe_model.ask``, one seat, no
    fallback: a failure is the seat's, not the next rung's."""
    me = _Seatless(num_ctx)
    backend, model = seat['backend'], seat['model']

    def call(messages, system):
        full = [{'role': 'system', 'content': system}] + messages
        if backend == 'ollama':
            body = CFOperator._ollama_chat_payload(me, model, full, 0.7)
            r = requests.post(f"{seat['url']}/api/chat", json=body, timeout=timeout)
            r.raise_for_status()
            text = (r.json().get('message') or {}).get('content', '')
        elif backend == 'anthropic':
            key = llm_keys.api_key(None, 'anthropic')
            if not key:
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            r = requests.post('https://api.anthropic.com/v1/messages', timeout=timeout, json={
                'model': model, 'max_tokens': 4096, 'system': system, 'messages': messages},
                headers={'x-api-key': key, 'anthropic-version': '2023-06-01',
                         'Content-Type': 'application/json'})
            r.raise_for_status()
            text = ''.join(b.get('text', '') for b in r.json().get('content', []) if b.get('type') == 'text')
        else:
            key, endpoint = CFOperator._openai_compat_request_config(me, backend)
            if not key:
                raise RuntimeError(f"{OPENAI_COMPAT_PROVIDERS[backend]['key_env']} not set")
            body = {'model': model, 'messages': full, 'temperature': 0.7, 'max_tokens': 4096,
                    **CFOperator._openai_compat_tool_loop_params(backend)}
            r = requests.post(endpoint, json=body, timeout=timeout,
                              headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
            r.raise_for_status()
            text = ((r.json().get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        return {'response': text, 'backend': backend, 'model': model}
    return call


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

#: Hand verdicts on folds the notes do not label (``--adjudication``):
#: S the same fix, an unmarked duplicate; B borderline, one covers more than
#: the other; D a different fix on the same subject, a real wrong fold.
_VERDICT = {'S': 'unmarked-dup', 'B': 'borderline'}


def grade(case: Dict[str, Any], answer: Dict[str, Any], adjudication: Optional[Dict[str, str]] = None) -> str:
    if answer['outcome'] in ('malformed', 'error'):
        return answer['outcome']
    row = answer.get('row_id')
    if case['kind'] == 'duplicate':
        if row is None:
            return 'missed'
        if row in case['expected']:
            return 'caught'
        g = 'wrong-row'
    else:
        if row is None:
            return 'correct'
        g = 'false-fold'
    if adjudication:
        g = _VERDICT.get(adjudication.get(f"{case['id']}-{row}"), g)
    return g


def run_seat(seat, cases, *, runs, workers, timeout, num_ctx, adjudication=None) -> Dict[str, Any]:
    call = caller(seat, timeout, num_ctx)

    def one(job):
        case, run = job
        start = time.monotonic()
        answer = dedupe_model.ask(call, case['candidate'], case['rows'], mode='eval')
        return {'case': case['id'], 'run': run, 'kind': case['kind'], 'expected': case['expected'],
                'row_id': answer.get('row_id'), 'reason': answer.get('reason'),
                'grade': grade(case, answer, adjudication), 'seconds': round(time.monotonic() - start, 2)}
    jobs = [(c, i) for c in cases for i in range(runs)]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(one, jobs))
    return {'seat': seat['name'], 'summary': summarize(results), 'results': results}


def summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    by = {g: sum(1 for r in results if r['grade'] == g)
          for g in ('caught', 'missed', 'wrong-row', 'correct', 'false-fold', 'unmarked-dup', 'borderline',
                    'malformed', 'error')}
    dup = sum(1 for r in results if r['kind'] == 'duplicate')
    distinct = len(results) - dup
    secs = sorted(r['seconds'] for r in results if r['grade'] != 'error') or [0.0]
    return {**by, 'duplicates': dup, 'distinct': distinct,
            # The number that decides: a wrong fold hides a real fix.
            'wrong_folds': by['false-fold'] + by['wrong-row'],
            'recall': round(by['caught'] / dup, 3) if dup else None,
            'p50_s': round(statistics.median(secs), 2),
            'p95_s': round(secs[min(len(secs) - 1, int(len(secs) * 0.95))], 2)}


def _rec(row_or_cand: Dict[str, Any]) -> str:
    p = row_or_cand.get('payload', row_or_cand)
    return ' '.join(str(p.get('recommendation') or '').split())[:220]


def report(out: Dict[str, Any], cases: List[Dict[str, Any]], adjudication: Optional[Dict[str, str]] = None) -> None:
    by_id = {c['id']: c for c in cases}
    print(f"\n{'seat':44} {'caught':>7} {'missed':>6} {'unmarked':>8} {'border':>6} {'WRONG':>5} "
          f"{'malf':>4} {'err':>3} {'p50':>6} {'p95':>6}")
    for s in out['seats']:
        m = s['summary']
        print(f"{s['seat'][:44]:44} {m['caught']:>3}/{m['duplicates']:<3} {m['missed']:>6} "
              f"{m['unmarked-dup']:>8} {m['borderline']:>6} {m['wrong_folds']:>5} {m['malformed']:>4} "
              f"{m['error']:>3} {m['p50_s']:>6} {m['p95_s']:>6}")
    for s in out['seats']:
        wrong = [r for r in s['results'] if r['grade'] in ('false-fold', 'wrong-row')]
        if not wrong:
            continue
        open_ = [r for r in wrong if (adjudication or {}).get(f"{r['case']}-{r['row_id']}") != 'D']
        print(f"\n== {s['seat']}: {len(wrong) - len(open_)} wrong fold(s) judged D, {len(open_)} with no "
              f"verdict yet (read each; an unmarked duplicate is not a model error)")
        for r in wrong:
            case = by_id[r['case']]
            named = next((x for x in case['rows'] if x['id'] == r['row_id']), {})
            verdict = 'D' if r not in open_ else 'no verdict'
            print(f"  #{r['case']} -> #{r['row_id']} ({r['grade']}, {verdict}): {r['reason']}")
            print(f"     new:   {_rec(case['candidate'])}")
            print(f"     named: {_rec(named)}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--rows', required=True, help='JSON from GET /api/remediations?limit=1000')
    ap.add_argument('--seat', action='append', default=[], help='backend:model, or ollama:model@url; repeatable')
    ap.add_argument('--negatives', type=int, default=60, help='distinct cases kept, hardest first')
    ap.add_argument('--runs', type=int, default=1, help='asks per case per seat')
    ap.add_argument('--workers', type=int, default=4, help='parallel asks per seat')
    ap.add_argument('--timeout', type=float, default=180.0)
    ap.add_argument('--num-ctx', type=int, default=None, help='mirror a deployment that sets llm.num_ctx')
    ap.add_argument('--limit', type=int, default=None, help='first N duplicate and N distinct cases (a smoke run)')
    ap.add_argument('--cases-only', action='store_true', help='print the cases and stop')
    ap.add_argument('--output', help='write the full result JSON here')
    ap.add_argument('--no-reasons', action='store_true',
                    help="leave the model's reasons out of --output: they describe the queue's real incidents, "
                         "and this repository is public")
    ap.add_argument('--adjudication', help='JSON of hand verdicts, {"<case>-<row>": "S"|"B"|"D"}')
    ap.add_argument('--replay', help='re-grade a result JSON written by --output; nothing is called')
    args = ap.parse_args(argv)
    adjudication = {k: v for k, v in json.load(open(args.adjudication)).items()
                    if not k.startswith('_')} if args.adjudication else None

    data = json.load(open(args.rows))
    rows = data if isinstance(data, list) else (data.get('remediations') or data.get('items') or [])
    cases = build_cases(rows, args.negatives)
    if args.limit:
        cases = ([c for c in cases if c['kind'] == 'duplicate'][:args.limit]
                 + [c for c in cases if c['kind'] == 'distinct'][:args.limit])
    dups = [c for c in cases if c['kind'] == 'duplicate']
    print(f"{len(rows)} rows -> {len(cases)} cases: {len(dups)} duplicate, {len(cases) - len(dups)} distinct "
          f"(hardest {args.negatives} kept)")
    if args.replay:
        out = json.load(open(args.replay))
        by_id = {c['id']: c for c in cases}
        for s in out['seats']:
            for r in s['results']:
                case = by_id.get(r['case'], {'id': r['case'], 'kind': r['kind'], 'expected': r['expected']})
                answer = {'outcome': 'error' if r['grade'] == 'error' else
                          'malformed' if r['grade'] == 'malformed' else 'fold', 'row_id': r['row_id']}
                r['grade'] = grade(case, answer, adjudication)
            s['summary'] = summarize(s['results'])
        report(out, cases, adjudication)
        return 0
    if args.cases_only:
        for c in cases:
            print(f"  #{c['id']:<4} {c['kind']:9} expects {c['expected'] or 'none'}  "
                  f"shown {len(c['rows'])}  hardness {c['hardness']}")
        return 0
    if not args.seat:
        ap.error('--seat is required unless --cases-only')
    seats = [parse_seat(s) for s in args.seat]
    out = {'date': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'rows': len(rows),
           'cases': len(cases), 'duplicates': len(dups), 'negatives_cap': args.negatives,
           'runs': args.runs, 'prompt': dedupe_model.SYSTEM_PROMPT, 'seats': []}
    # Seats are independent, so they run side by side; each keeps its own
    # --workers, and the order in the report is the order on the command line.
    def seat_run(seat):
        print(f"... {seat['name']}", file=sys.stderr, flush=True)
        return run_seat(seat, cases, runs=args.runs, workers=args.workers,
                        timeout=args.timeout, num_ctx=args.num_ctx, adjudication=adjudication)
    with ThreadPoolExecutor(max_workers=len(seats)) as pool:
        out['seats'] = list(pool.map(seat_run, seats))
    report(out, cases, adjudication)
    if args.output:
        if args.no_reasons:
            for s in out['seats']:
                for r in s['results']:
                    r.pop('reason', None)
        with open(args.output, 'w') as f:
            json.dump(out, f, indent=1)
        print(f"\nwrote {args.output}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
