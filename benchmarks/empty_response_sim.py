#!/usr/bin/env python3
"""
Empty-final-response simulation for the investigation tool-calling loop.

Reproduces the agent's `_chat_with_tools_inner` Ollama path (agent/agent.py)
against a live Ollama server, but with CANNED tool results — no cluster
access — so every trial is identical and any variance is the model's.

Motivation: investigations #1880/#1884/#1885/#1889 (2026-07-10) completed
with an empty final response on gemma4:26b; `_extract_status("")` then
silently defaulted the outcome to 'monitoring'. This measures how often
each model ends the loop with a message that has neither tool_calls nor
content — the exact failure signature.

Faithful to production:
  - real 50-tool schema payload (investigation_tool_schemas.json, dumped
    from the deployed pod's ToolRegistry.get_schemas())
  - same investigation system prompt skeleton incl. STATUS/RECOMMENDATION
    contract (agent.py ~1028) and a similar-investigations block
  - temperature 0.7, max 10 iterations, tools withheld on final iteration
  - memoization stub for repeated identical read-only calls
  - tool results serialized/truncated like _serialize_tool_result (6000 chars)

Not reproduced: real learnings text (varies per run), live cluster data.

Usage:
    python benchmarks/empty_response_sim.py \
        [--url http://ubuntu-llm-01:11434] \
        [--models gemma4:26b,qwen3.6:27b,glm-4.7-flash:latest] \
        [--trials 10] \
        [--output benchmarks/empty_response_sim_results.jsonl]
"""

import argparse
import json
import time
import requests

MAX_ITERATIONS = 10
MAX_RESULT_CHARS = 6000
LLM_TIMEOUT = 180

# Keep in sync with agent.EMPTY_RESPONSE_NUDGE — this is the wording that
# ships; the sim validates it recovers gemma4's empty finals.
NUDGE = ("You have gathered enough data. Do NOT call any more tools. "
         "Respond NOW with a short summary of what you found, followed by your "
         "final answer in exactly the format the instructions above require.")

MEMOIZABLE = frozenset({
    'k8s_get_pods', 'k8s_get_nodes', 'k8s_get_deployments', 'k8s_get_events',
    'k8s_get_all_unhealthy', 'k8s_get_ingresses', 'k8s_get_services',
    'k8s_get_pod_status', 'k8s_get_pod_logs', 'loki_query', 'prometheus_query',
    'ssh_list_services', 'ping_host',
})

TRIGGER = ("Verify Loki stability: Check logs for Loki to ensure no more "
           "503 Service Unavailable errors are occurring")

# Mirrors the similar_text block hybrid search injects (agent.py ~1006).
SIMILAR_TEXT = """

Similar past investigations:
- [monitoring] Loki 503 Service Unavailable errors on log push from promtail (similarity: 0.82)
- [resolved] Loki pod restart loop after PVC pressure on raspberrypi (similarity: 0.74)
- [monitoring] Per-host backup stale on raspberrypi2 (similarity: 0.61)
"""

SYSTEM_PROMPT = f"""You are CFOperator investigating an infrastructure alert.

Alert: {TRIGGER}
Alert details: {{}}
{SIMILAR_TEXT}
Investigate this alert using the available tools. Check metrics, logs, and container/service status.
First give a short summary of what you found. Then end your response with exactly these two lines:

STATUS: <one of: resolved | needs_action | monitoring | escalate>
  - resolved: the resource is healthy RIGHT NOW — the problem is gone, or you fixed it during this investigation. Do NOT use resolved just because you identified a fix that someone still has to apply.
  - needs_action: you found the problem but it needs a change you could not make yourself; your RECOMMENDATION says what to do.
  - monitoring: transient or inconclusive; worth watching, no action yet.
  - escalate: urgent; a human should look now.
RECOMMENDATION: <the single most useful operator-facing next step — a concrete command or config change, or "No action needed" when the resource is genuinely healthy>"""

USER_MESSAGE = f"Investigate this alert: {TRIGGER}"

# ── Canned tool results: healthy-Loki scenario ────────────────────────────────

LOKI_LOG_LINES = [
    "level=info ts=2026-07-10T10:58:12.4Z caller=table_manager.go:171 msg=\"handing over indexes to shipper\"",
    "level=info ts=2026-07-10T10:59:02.1Z caller=checkpoint.go:615 msg=\"starting checkpoint\"",
    "level=info ts=2026-07-10T10:59:02.9Z caller=checkpoint.go:340 msg=\"completed checkpoint\"",
    "level=info ts=2026-07-10T11:00:44.7Z caller=flush.go:167 msg=\"flushing stream\" user=fake",
    "level=info ts=2026-07-10T11:01:31.2Z caller=metrics.go:159 component=frontend status=200 duration=48ms",
]

CANNED = {
    'k8s_get_pods': {
        'namespace': 'monitoring',
        'pods': [
            {'name': 'loki-0', 'status': 'Running', 'ready': '1/1', 'restarts': 0,
             'age': '5h19m', 'node': 'raspberrypi'},
            {'name': 'prometheus-0', 'status': 'Running', 'ready': '2/2', 'restarts': 0,
             'age': '17h', 'node': 'ubuntu-llm-01'},
            {'name': 'grafana-6f5c9d7b4-kx2mp', 'status': 'Running', 'ready': '1/1',
             'restarts': 0, 'age': '17h', 'node': 'ubuntu-cm5-01'},
            {'name': 'promtail-fdppw', 'status': 'Running', 'ready': '1/1', 'restarts': 30,
             'age': '42d', 'node': 'raspberrypi'},
            {'name': 'promtail-mb6j7', 'status': 'Running', 'ready': '1/1', 'restarts': 21,
             'age': '42d', 'node': 'raspberrypi2'},
        ],
    },
    'k8s_get_pod_status': {
        'pod': 'loki-0', 'namespace': 'monitoring', 'phase': 'Running',
        'ready': True, 'restarts': 0, 'started_at': '2026-07-10T05:43:21Z',
        'conditions': [{'type': 'Ready', 'status': 'True'}],
    },
    'k8s_get_pod_logs': {
        'pod': 'loki-0', 'namespace': 'monitoring', 'lines': LOKI_LOG_LINES,
        'note': 'no 503 or error entries in the requested window',
    },
    'k8s_get_services': {
        'namespace': 'monitoring',
        'services': [
            {'name': 'loki', 'type': 'ClusterIP', 'cluster_ip': '10.43.112.7',
             'ports': ['3100/TCP'], 'endpoints': ['10.42.0.96:3100']},
            {'name': 'prometheus', 'type': 'ClusterIP', 'cluster_ip': '10.43.88.14',
             'ports': ['9090/TCP'], 'endpoints': ['10.42.3.41:9090']},
        ],
    },
    'k8s_describe': {
        'kind': 'Pod', 'name': 'loki-0', 'namespace': 'monitoring',
        'status': 'Running', 'restarts': 0,
        'events': 'no events in the last hour',
        'containers': [{'name': 'loki', 'image': 'grafana/loki:2.9.6',
                        'state': 'running', 'last_state': 'terminated (exit 0, 5h ago)'}],
    },
    'k8s_get_events': {'namespace': 'monitoring', 'events': [],
                       'note': 'no warning events in the last hour'},
    'prometheus_query': {
        'status': 'success',
        'result': [{'metric': {'job': 'loki', 'status_code': '503'},
                    'value': [1783335760, '0']}],
        'note': 'zero 503 responses recorded over the queried window',
    },
    'loki_query': {
        'streams': [{'labels': '{container_name="loki"}', 'lines': LOKI_LOG_LINES}],
        'note': 'no matches for "503" in the requested window',
    },
}

DEFAULT_RESULT = {'status': 'ok', 'note': 'no anomalies found'}


def serialize(result):
    text = json.dumps(result, default=str)
    if len(text) <= MAX_RESULT_CHARS:
        return text
    omitted = len(text) - MAX_RESULT_CHARS
    return text[:MAX_RESULT_CHARS] + f'\n...[truncated {omitted} chars of tool output]'


def extract_status(response_text):
    """Copy of agent._extract_status classification."""
    text = response_text or ""
    idx = text.lower().rfind('status:')
    if idx != -1:
        line = text[idx + len('status:'):].split('\n', 1)[0].lower()
        if any(k in line for k in ('needs_action', 'needs-action', 'needs action',
                                   'unresolved', 'action needed')):
            return 'needs_action'
        if any(k in line for k in ('escalate', 'escalated', 'urgent')):
            return 'escalated'
        if 'monitor' in line:
            return 'monitoring'
        if any(k in line for k in ('resolved', 'fixed', 'healthy', 'no action', 'no issue')):
            return 'resolved'
    low = text.lower()
    if any(w in low for w in ('escalat', 'urgent')):
        return 'escalated'
    return 'monitoring'


def nudge_retry(url, model, tools, messages, retries, offer_tools):
    """On an empty final answer, append an explicit nudge and re-ask.

    Returns (recovered_text or '', attempts_used, rounds_log). offer_tools
    controls whether the retry request still presents the tool schemas —
    some models answer tool-less requests with empty content, so both
    variants are worth measuring.
    """
    msgs = list(messages)
    rounds = []
    for attempt in range(1, retries + 1):
        msgs.append({'role': 'user', 'content': NUDGE})
        payload = {'model': model, 'messages': msgs, 'stream': False,
                   'temperature': 0.7}
        if offer_tools:
            payload['tools'] = tools
        resp = requests.post(f"{url}/api/chat", json=payload, timeout=LLM_TIMEOUT)
        resp.raise_for_status()
        message = resp.json().get('message', {})
        tool_calls = message.get('tool_calls', [])
        content = (message.get('content', '') or '').strip()
        rounds.append({'attempt': attempt,
                       'tool_calls': [tc['function']['name'] for tc in tool_calls],
                       'content_len': len(content)})
        if content:
            return content, attempt, rounds
        # If it tried to call tools instead of answering, refuse and re-nudge.
        msgs.append({'role': 'assistant', 'content': content})
    return '', retries, rounds


def run_trial(url, model, tools, trial_idx, nudge_retries=0, nudge_with_tools=False):
    messages = [{'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': USER_MESSAGE}]
    tool_calls_count = 0
    cache = {}
    rounds = []
    start = time.time()
    final_text = None
    hit_limit = False

    for iteration in range(MAX_ITERATIONS):
        is_final = (iteration == MAX_ITERATIONS - 1)
        payload = {'model': model, 'messages': messages, 'stream': False,
                   'temperature': 0.7}
        if not is_final:
            payload['tools'] = tools
        resp = requests.post(f"{url}/api/chat", json=payload, timeout=LLM_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        message = data.get('message', {})
        tool_calls = message.get('tool_calls', [])
        content = message.get('content', '') or ''
        rounds.append({'iteration': iteration + 1,
                       'tool_calls': [tc['function']['name'] for tc in tool_calls],
                       'content_len': len(content)})

        if tool_calls:
            messages.append(message)
            for tc in tool_calls:
                name = tc['function']['name']
                raw_args = tc['function'].get('arguments', {})
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args) if raw_args.strip() else {}
                    except json.JSONDecodeError:
                        args = {}
                else:
                    args = raw_args or {}
                key = None
                if name in MEMOIZABLE:
                    key = name + '|' + json.dumps(args, sort_keys=True, default=str)
                if key is not None and key in cache:
                    content_str = json.dumps({
                        'cached': True,
                        'note': (f"Identical {name} call already made in this session — "
                                 f"result is unchanged. Reuse the earlier {name} output "
                                 f"above instead of re-fetching.")})
                else:
                    result = CANNED.get(name, DEFAULT_RESULT)
                    if key is not None:
                        cache[key] = result
                    content_str = serialize(result)
                tool_calls_count += 1
                messages.append({'role': 'tool', 'content': content_str})
            continue

        final_text = content
        hit_limit = is_final
        break
    else:  # pragma: no cover — loop always breaks on the tool-less final round
        final_text = ''

    ft = final_text or ''
    result = {
        'model': model,
        'trial': trial_idx,
        'tool_calls': tool_calls_count,
        'llm_rounds': len(rounds),
        'hit_iteration_limit': hit_limit,
        'response_len': len(ft),
        'empty_response': len(ft.strip()) == 0,
        'has_status_line': 'status:' in ft.lower(),
        'extracted_status': extract_status(ft),
        'rounds': rounds,
        'response_head': ft[:400],
    }

    if result['empty_response'] and nudge_retries > 0:
        recovered, attempts, nudge_rounds = nudge_retry(
            url, model, tools, messages, nudge_retries, nudge_with_tools)
        result.update({
            'nudge_attempts': attempts,
            'nudge_rounds': nudge_rounds,
            'nudge_recovered': bool(recovered),
            'nudge_response_len': len(recovered),
            'nudge_has_status_line': 'status:' in recovered.lower(),
            'nudge_extracted_status': extract_status(recovered) if recovered else None,
            'nudge_response_head': recovered[:400],
        })

    result['duration_s'] = round(time.time() - start, 1)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', default='http://ubuntu-llm-01:11434')
    ap.add_argument('--models', default='gemma4:26b,qwen3.6:27b,glm-4.7-flash:latest')
    ap.add_argument('--trials', type=int, default=10)
    ap.add_argument('--schemas', default='benchmarks/investigation_tool_schemas.json')
    ap.add_argument('--output', default='benchmarks/empty_response_sim_results.jsonl')
    ap.add_argument('--nudge-retries', type=int, default=0,
                    help='on empty final answer, nudge-and-retry up to N times')
    ap.add_argument('--nudge-with-tools', action='store_true',
                    help='keep tool schemas in the nudge retry request')
    args = ap.parse_args()

    with open(args.schemas) as f:
        tools = json.load(f)
    models = [m.strip() for m in args.models.split(',') if m.strip()]

    all_results = []
    with open(args.output, 'w') as out:
        for model in models:
            print(f"\n=== {model} ===", flush=True)
            for i in range(args.trials):
                try:
                    r = run_trial(args.url, model, tools, i + 1,
                                  nudge_retries=args.nudge_retries,
                                  nudge_with_tools=args.nudge_with_tools)
                except Exception as e:
                    r = {'model': model, 'trial': i + 1, 'error': f"{type(e).__name__}: {e}"}
                all_results.append(r)
                out.write(json.dumps(r) + '\n')
                out.flush()
                if 'error' in r:
                    print(f"  trial {i+1}: ERROR {r['error']}", flush=True)
                else:
                    nudge = ''
                    if r.get('empty_response') and 'nudge_recovered' in r:
                        nudge = (f" | nudge: recovered={r['nudge_recovered']} "
                                 f"attempts={r['nudge_attempts']} "
                                 f"status_line={r.get('nudge_has_status_line')}")
                    print(f"  trial {i+1}: empty={r['empty_response']} "
                          f"status_line={r['has_status_line']} -> {r['extracted_status']} "
                          f"({r['tool_calls']} calls, {r['llm_rounds']} rounds, "
                          f"{r['duration_s']}s){nudge}", flush=True)

    print("\n=== SUMMARY ===")
    for model in models:
        rs = [r for r in all_results if r['model'] == model and 'error' not in r]
        errs = [r for r in all_results if r['model'] == model and 'error' in r]
        if not rs:
            print(f"{model}: all {len(errs)} trials errored")
            continue
        empty = sum(1 for r in rs if r['empty_response'])
        no_status = sum(1 for r in rs if not r['has_status_line'])
        limit = sum(1 for r in rs if r['hit_iteration_limit'])
        avg_calls = sum(r['tool_calls'] for r in rs) / len(rs)
        line = (f"{model}: {empty}/{len(rs)} empty responses, "
                f"{no_status}/{len(rs)} missing STATUS line, "
                f"{limit}/{len(rs)} hit iteration limit, "
                f"avg {avg_calls:.1f} tool calls"
                + (f", {len(errs)} errored" if errs else ""))
        nudged = [r for r in rs if 'nudge_recovered' in r]
        if nudged:
            rec = sum(1 for r in nudged if r['nudge_recovered'])
            with_status = sum(1 for r in nudged if r.get('nudge_has_status_line'))
            line += (f" | nudge: {rec}/{len(nudged)} recovered, "
                     f"{with_status}/{len(nudged)} with STATUS line")
        print(line)


if __name__ == '__main__':
    main()
