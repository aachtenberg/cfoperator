#!/usr/bin/env python3
"""
Test Ollama models for tool-calling capability.

Runs 3 tests per model:
  T1 - Single tool call: "What's the CPU usage?" with prometheus_query
  T2 - Multi-turn: Feed back a tool result, see if model continues with another call
  T3 - Tool selection: Multiple tools available, must pick the right one

Scoring per test (0-2):
  0 = no tool call / garbled
  1 = tool call but wrong name or bad args
  2 = correct tool call with reasonable args

Total score: 0-6 per model, normalized to 0-10 for final report.
"""

import json
import time
import sys
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

HOSTS = {
    'ollama-gpu':     'http://10.0.0.5:11434',
    'ollama-198':     'http://10.0.0.6:11434',
    'ollama-desktop': 'http://10.0.0.8:11434',
}

# Models to skip (embedding, base, vision-only)
SKIP_MODELS = {
    'nomic-embed-text:latest',
    'qwen2.5-coder:1.5b-base',
}

# Simplified tool schemas (matching CFOperator style)
TOOL_PROM = {
    'type': 'function',
    'function': {
        'name': 'prometheus_query',
        'description': 'Query Prometheus metrics. Returns time-series data.',
        'parameters': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'PromQL query string'},
                'duration': {'type': 'string', 'description': 'Time range, e.g. 1h, 30m'}
            },
            'required': ['query']
        }
    }
}

TOOL_LOKI = {
    'type': 'function',
    'function': {
        'name': 'loki_query',
        'description': 'Query Loki logs. Use labels: host, container_name. Example: {host="raspberrypi"} |= "error"',
        'parameters': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'LogQL query string'},
                'limit': {'type': 'integer', 'description': 'Max log lines'}
            },
            'required': ['query']
        }
    }
}

TOOL_DOCKER = {
    'type': 'function',
    'function': {
        'name': 'docker_list',
        'description': 'List Docker containers on a host. Returns container names, status, images.',
        'parameters': {
            'type': 'object',
            'properties': {
                'host': {'type': 'string', 'description': 'Hostname to query'}
            },
            'required': ['host']
        }
    }
}

SYSTEM = "You are an infrastructure monitoring agent. Use the provided tools to investigate. Do NOT guess answers — always call a tool first."

TIMEOUT = 180  # seconds per API call


def call_ollama(url, model, messages, tools, timeout=TIMEOUT):
    """Send a chat request to Ollama and return the parsed response."""
    payload = {
        'model': model,
        'messages': messages,
        'tools': tools,
        'stream': False,
        'temperature': 0.3,
    }
    try:
        r = requests.post(f"{url}/api/chat", json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {'error': str(e)}


def extract_tool_call(data):
    """Extract tool call info from Ollama response. Returns (name, args) or (None, None)."""
    if 'error' in data:
        return None, None
    msg = data.get('message', {})
    tcs = msg.get('tool_calls', [])
    if not tcs:
        return None, None
    tc = tcs[0]
    fn = tc.get('function', {})
    return fn.get('name'), fn.get('arguments', {})


def test1_single_call(url, model):
    """T1: Does the model make a prometheus_query tool call?"""
    messages = [
        {'role': 'system', 'content': SYSTEM},
        {'role': 'user', 'content': 'What is the current CPU usage on raspberrypi?'}
    ]
    data = call_ollama(url, model, messages, [TOOL_PROM])
    name, args = extract_tool_call(data)

    if name is None:
        # Check if model put tool call in content as text (common failure mode)
        content = data.get('message', {}).get('content', '')
        if 'prometheus_query' in content.lower():
            return 1, 'tool name in text but not structured', data
        return 0, 'no tool call', data

    if name != 'prometheus_query':
        return 1, f'wrong tool: {name}', data

    query = args.get('query', '')
    if not query:
        return 1, 'correct tool but empty query', data

    # Check if query looks like valid PromQL
    cpu_keywords = ['cpu', 'node_cpu', 'system_cpu', 'load', 'idle']
    if any(k in query.lower() for k in cpu_keywords):
        return 2, f'correct: {query}', data
    return 1, f'correct tool but odd query: {query}', data


def test2_multi_turn(url, model):
    """T2: After getting a tool result, does the model make another tool call?"""
    messages = [
        {'role': 'system', 'content': SYSTEM},
        {'role': 'user', 'content': 'Check CPU usage and then check for any error logs on raspberrypi.'},
    ]
    # First call — expect prometheus_query
    data1 = call_ollama(url, model, messages, [TOOL_PROM, TOOL_LOKI])
    name1, args1 = extract_tool_call(data1)

    if name1 is None:
        return 0, 'no tool call on turn 1', data1

    # Feed back a fake tool result
    messages.append(data1.get('message', {}))
    messages.append({
        'role': 'tool',
        'content': json.dumps({
            'status': 'success',
            'data': {'resultType': 'vector', 'result': [{'value': [1707000000, '0.23']}]}
        })
    })

    data2 = call_ollama(url, model, messages, [TOOL_PROM, TOOL_LOKI])
    name2, args2 = extract_tool_call(data2)

    if name2 is None:
        content = data2.get('message', {}).get('content', '')
        if len(content) > 20:
            return 1, f'1 tool call then text response (no 2nd call)', data2
        return 0, 'no 2nd tool call, short response', data2

    if name2 == name1:
        return 1, f'2nd call same tool ({name2}), wanted different', data2

    return 2, f'multi-turn works: {name1} → {name2}', data2


def test3_tool_selection(url, model):
    """T3: Given 3 tools, does the model pick the right one for a log query?"""
    messages = [
        {'role': 'system', 'content': SYSTEM},
        {'role': 'user', 'content': 'Show me recent error logs from the immich_server container.'}
    ]
    data = call_ollama(url, model, messages, [TOOL_PROM, TOOL_LOKI, TOOL_DOCKER])
    name, args = extract_tool_call(data)

    if name is None:
        return 0, 'no tool call', data

    if name != 'loki_query':
        return 1, f'wrong tool: {name} (expected loki_query)', data

    query = args.get('query', '')
    if 'immich' in query.lower() and ('error' in query.lower() or '|=' in query):
        return 2, f'correct: {query}', data
    elif 'immich' in query.lower() or 'error' in query.lower():
        return 1, f'partial query: {query}', data

    return 1, f'loki_query but odd query: {query}', data


def test_model(host_name, url, model):
    """Run all 3 tests on a model. Returns dict of results."""
    print(f"  Testing {model} on {host_name}...", flush=True)
    start = time.time()

    s1, d1, _ = test1_single_call(url, model)
    s2, d2, _ = test2_multi_turn(url, model)
    s3, d3, _ = test3_tool_selection(url, model)

    raw_total = s1 + s2 + s3  # 0-6
    score_10 = round(raw_total / 6 * 10, 1)
    elapsed = round(time.time() - start, 1)

    result = {
        'model': model,
        'host': host_name,
        'T1_single': {'score': s1, 'detail': d1},
        'T2_multi':  {'score': s2, 'detail': d2},
        'T3_select': {'score': s3, 'detail': d3},
        'raw': raw_total,
        'score': score_10,
        'time_s': elapsed,
    }
    grade = '★' * int(score_10 // 2) + '☆' * (5 - int(score_10 // 2))
    print(f"    {model}: {score_10}/10 {grade} ({elapsed}s) — T1={s1} T2={s2} T3={s3}", flush=True)
    return result


def get_models(url):
    """Fetch model list from an Ollama instance."""
    try:
        r = requests.get(f"{url}/api/tags", timeout=10)
        r.raise_for_status()
        return [m['name'] for m in r.json().get('models', []) if m['name'] not in SKIP_MODELS]
    except Exception as e:
        print(f"  Error fetching models from {url}: {e}")
        return []


def test_host(host_name, url):
    """Test all models on a single host sequentially."""
    models = get_models(url)
    print(f"\n{'='*60}")
    print(f"HOST: {host_name} ({url}) — {len(models)} models")
    print(f"{'='*60}")
    results = []
    for model in models:
        try:
            r = test_model(host_name, url, model)
            results.append(r)
        except Exception as e:
            print(f"    {model}: FAILED — {e}")
            results.append({
                'model': model, 'host': host_name,
                'T1_single': {'score': 0, 'detail': str(e)},
                'T2_multi': {'score': 0, 'detail': 'skipped'},
                'T3_select': {'score': 0, 'detail': 'skipped'},
                'raw': 0, 'score': 0, 'time_s': 0,
            })
    return results


# ── Parallel vs Sequential Sweep Comparison ──────────────────────────────

# Pool config — matches config.yaml ollama_pool
POOL = [
    {'name': 'ollama-gpu',     'url': 'http://10.0.0.5:11434', 'model': 'mistral-small3.2:24b'},
    {'name': 'ollama-198',     'url': 'http://10.0.0.6:11434', 'model': 'qwen2.5:7b-instruct-q8_0'},
    {'name': 'ollama-desktop', 'url': 'http://10.0.0.8:11434', 'model': 'ministral-3:latest'},
]

# 3 sweep phases — each one a realistic monitoring prompt with all tools available
PHASES = [
    {
        'name': 'metrics',
        'prompt': 'Check CPU and memory usage across all hosts. Are any hosts over 80% utilization?',
        'expected_tool': 'prometheus_query',
        'fake_result': json.dumps({
            'status': 'success',
            'data': {'resultType': 'vector', 'result': [
                {'metric': {'instance': 'raspberrypi'}, 'value': [1707000000, '67.3']},
                {'metric': {'instance': 'raspberrypi3'}, 'value': [1707000000, '42.1']},
                {'metric': {'instance': 'ollama-gpu'}, 'value': [1707000000, '89.7']},
                {'metric': {'instance': 'pi2'}, 'value': [1707000000, '31.5']},
            ]}
        }),
    },
    {
        'name': 'logs',
        'prompt': 'Search for recent error logs across all containers in the last hour.',
        'expected_tool': 'loki_query',
        'fake_result': json.dumps({
            'status': 'success',
            'data': {'result': [
                {'stream': {'container_name': 'immich_server'}, 'values': [
                    ['1707000100', 'ERROR: database connection timeout after 30s'],
                    ['1707000200', 'ERROR: failed to process upload batch - retrying'],
                ]},
                {'stream': {'container_name': 'nginx'}, 'values': [
                    ['1707000150', 'ERROR: upstream timed out (110: Connection timed out)'],
                ]},
            ]}
        }),
    },
    {
        'name': 'containers',
        'prompt': 'List all Docker containers on raspberrypi and check if any are unhealthy or restarting.',
        'expected_tool': 'docker_list',
        'fake_result': json.dumps({
            'status': 'success',
            'containers': [
                {'name': 'prometheus', 'status': 'Up 14 days', 'health': 'healthy'},
                {'name': 'loki', 'status': 'Up 14 days', 'health': 'healthy'},
                {'name': 'grafana', 'status': 'Up 14 days', 'health': 'healthy'},
                {'name': 'postgres', 'status': 'Up 14 days', 'health': 'healthy'},
                {'name': 'mosquitto', 'status': 'Restarting (1) 5 minutes ago', 'health': 'unhealthy'},
                {'name': 'cloudflared', 'status': 'Up 14 days', 'health': 'healthy'},
            ]
        }),
    },
]

ALL_TOOLS = [TOOL_PROM, TOOL_LOKI, TOOL_DOCKER]


def run_phase(host_name, url, model, phase):
    """Run a single sweep phase (multi-turn: tool call → result → analysis).
    Mirrors what the real LangGraph sweep does per phase."""
    messages = [
        {'role': 'system', 'content': SYSTEM},
        {'role': 'user', 'content': phase['prompt']},
    ]

    start = time.time()

    # Turn 1: model should make a tool call
    data1 = call_ollama(url, model, messages, ALL_TOOLS)
    name, args = extract_tool_call(data1)
    tool_ok = name == phase['expected_tool']
    query = args.get('query', args.get('host', '')) if args else ''

    finding = ''
    if name:
        # Turn 2: feed back fake tool result, model analyzes
        messages.append(data1.get('message', {}))
        messages.append({'role': 'tool', 'content': phase['fake_result']})
        data2 = call_ollama(url, model, messages, ALL_TOOLS)
        finding = data2.get('message', {}).get('content', '')[:200]

    elapsed = round(time.time() - start, 1)

    return {
        'phase': phase['name'],
        'host': host_name,
        'model': model,
        'time_s': elapsed,
        'tool_called': name,
        'expected': phase['expected_tool'],
        'correct': tool_ok,
        'query': query,
        'finding': finding,
    }


def run_sequential(single_host):
    """Run all 3 phases sequentially on ONE Ollama instance."""
    results = []
    for phase in PHASES:
        r = run_phase(single_host['name'], single_host['url'], single_host['model'], phase)
        results.append(r)
    return results


def run_parallel():
    """Run each phase on a different Ollama instance concurrently."""
    results = [None] * 3
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {}
        for i, (host, phase) in enumerate(zip(POOL, PHASES)):
            f = pool.submit(run_phase, host['name'], host['url'], host['model'], phase)
            futures[f] = i
        for future in as_completed(futures):
            idx = futures[future]
            results[idx] = future.result()
    return results


def print_phase_table(results, is_parallel=False):
    """Print a phase results table with findings."""
    total = round(sum(r['time_s'] for r in results), 1)
    wall = max(r['time_s'] for r in results) if is_parallel else total
    correct = sum(1 for r in results if r['correct'])

    print(f"\n  {'Phase':<12} {'Host':<16} {'Model':<30} {'Time':>7}  {'Tool':>4}  Tool Called")
    print(f"  {'─'*100}")
    for r in results:
        check = '  OK' if r['correct'] else ' BAD'
        tool_str = f"{r['tool_called'] or '(none)'}"
        print(f"  {r['phase']:<12} {r['host']:<16} {r['model']:<30} {r['time_s']:>6}s  {check}  {tool_str}")
        if r.get('finding'):
            finding = r['finding'].replace('\n', ' ').strip()
            print(f"               Finding: {finding[:90]}")
            if len(finding) > 90:
                print(f"                        {finding[90:180]}")
    print(f"  {'─'*100}")
    if is_parallel:
        print(f"  Wall clock:             {wall:>6}s  (= longest phase, others run concurrently)")
    else:
        print(f"  Wall clock:             {wall:>6}s  (= sum of phases, ran sequentially)")
    print(f"  Correct tool calls:        {correct}/3")
    return wall, total, correct


def run_sweep_comparison():
    """Compare parallel (3 hosts) vs sequential (1 host) sweep timing."""
    print(f"\n{'='*100}")
    print("PARALLEL vs SEQUENTIAL SWEEP COMPARISON")
    print(f"{'='*100}")
    print(f"\nPool: {len(POOL)} Ollama instances")
    for h in POOL:
        print(f"  {h['name']:<16} {h['url']:<34} {h['model']}")
    print(f"\nPhases: {len(PHASES)}")
    for p in PHASES:
        print(f"  {p['name']:<12} → expects {p['expected_tool']}")

    # ── Sequential: all 3 phases on each host (shows per-host baseline) ──
    seq_all = {}
    for host in POOL:
        print(f"\n{'─'*100}")
        print(f"SEQUENTIAL — all 3 phases on {host['name']} ({host['model']})")
        print(f"{'─'*100}")

        s_start = time.time()
        s_results = run_sequential(host)
        s_wall = round(time.time() - s_start, 1)

        _, s_sum, s_correct = print_phase_table(s_results, is_parallel=False)
        seq_all[host['name']] = {'wall_s': s_wall, 'sum_s': s_sum, 'correct': s_correct, 'phases': s_results}

    # Use the GPU host as the "single instance" baseline (typical default)
    single = POOL[0]
    seq_results = seq_all[single['name']]['phases']
    seq_wall = seq_all[single['name']]['wall_s']
    seq_sum = seq_all[single['name']]['sum_s']
    seq_correct = seq_all[single['name']]['correct']

    # ── Parallel: one phase per host ──
    print(f"\n{'─'*100}")
    print(f"PARALLEL — 1 phase per host (fan-out to {len(POOL)} instances)")
    print(f"{'─'*100}")

    par_start = time.time()
    par_results = run_parallel()
    par_wall = round(time.time() - par_start, 1)

    par_longest, par_sum, par_correct = print_phase_table(par_results, is_parallel=True)

    # ── Sequential baselines summary ──
    print(f"\n{'─'*100}")
    print("SEQUENTIAL BASELINES (if you only had 1 Ollama instance)")
    print(f"{'─'*100}")
    print(f"\n  {'Host':<16} {'Model':<30} {'Wall Clock':>10}  {'Tool Calls':>10}")
    print(f"  {'─'*70}")
    for host in POOL:
        s = seq_all[host['name']]
        print(f"  {host['name']:<16} {host['model']:<30} {s['wall_s']:>8}s  {s['correct']:>6}/3")
    avg_seq = round(sum(s['wall_s'] for s in seq_all.values()) / len(seq_all), 1)
    print(f"  {'─'*70}")
    print(f"  {'Average sequential:':<48} {avg_seq:>8}s")

    # ── Comparison ──
    speedup = round(avg_seq / par_wall, 1) if par_wall > 0 else 0
    saved = round(avg_seq - par_wall, 1)
    pct = round((1 - par_wall / avg_seq) * 100) if avg_seq > 0 else 0

    print(f"\n{'='*100}")
    print("RESULTS COMPARISON")
    print(f"{'='*100}")
    print(f"""
  ┌────────────────────────────┬───────────────────┬───────────────────┐
  │                            │    Sequential     │     Parallel      │
  │                            │   (1 instance)    │  (3 instances)    │
  ├────────────────────────────┼───────────────────┼───────────────────┤
  │ Wall clock time            │     {avg_seq:>6}s        │     {par_wall:>6}s        │
  │ Correct tool calls         │        3/3          │      {par_correct}/3          │
  │ Instances used             │         1           │         3           │
  ├────────────────────────────┼───────────────────┴───────────────────┤
  │ Speedup                    │  {speedup}x faster ({pct}% wall-clock reduction)  │
  │ Time saved                 │  {saved}s per sweep cycle                     │
  └────────────────────────────┴───────────────────────────────────────┘

  Sequential = avg wall clock across {len(POOL)} hosts running all 3 phases alone
  Parallel   = wall clock when fanning out 1 phase per host concurrently
""")

    return {
        'sequential_baselines': {
            name: {'wall_s': s['wall_s'], 'correct': s['correct'],
                   'phases': s['phases']}
            for name, s in seq_all.items()
        },
        'sequential_avg_wall_s': avg_seq,
        'parallel': {'wall_s': par_wall, 'phases': par_results, 'correct': par_correct},
        'speedup': speedup,
        'time_saved_s': saved,
    }


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'all'

    if mode in ('all', 'models'):
        print("=" * 60)
        print("PART 1: MODEL TOOL-CALLING BENCHMARK")
        print(f"Testing {len(HOSTS)} hosts, {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)

        all_results = []

        # Test hosts in parallel (models within a host are sequential)
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(test_host, name, url): name for name, url in HOSTS.items()}
            for future in as_completed(futures):
                host = futures[future]
                try:
                    results = future.result()
                    all_results.extend(results)
                except Exception as e:
                    print(f"Host {host} failed: {e}")

        # Sort by score descending
        all_results.sort(key=lambda x: x['score'], reverse=True)

        # Print summary table
        print(f"\n{'='*80}")
        print("FINAL RANKINGS")
        print(f"{'='*80}")
        print(f"{'Rank':<5} {'Model':<40} {'Host':<16} {'T1':>3} {'T2':>3} {'T3':>3} {'Score':>6} {'Time':>6}")
        print("-" * 80)
        for i, r in enumerate(all_results, 1):
            print(f"{i:<5} {r['model']:<40} {r['host']:<16} {r['T1_single']['score']:>3} {r['T2_multi']['score']:>3} {r['T3_select']['score']:>3} {r['score']:>5}/10 {r['time_s']:>5}s")

        print(f"\n{'='*80}")
        print("DETAILED RESULTS")
        print(f"{'='*80}")
        for r in all_results:
            print(f"\n{r['model']} ({r['host']}): {r['score']}/10")
            print(f"  T1 Single call:    [{r['T1_single']['score']}/2] {r['T1_single']['detail']}")
            print(f"  T2 Multi-turn:     [{r['T2_multi']['score']}/2] {r['T2_multi']['detail']}")
            print(f"  T3 Tool selection: [{r['T3_select']['score']}/2] {r['T3_select']['detail']}")

        # Save JSON results
        with open('tool_calling_results.json', 'w') as f:
            json.dump(all_results, f, indent=2)
        print(f"\nResults saved to tool_calling_results.json")

    if mode in ('all', 'sweep'):
        sweep = run_sweep_comparison()

        # Save sweep results
        with open('sweep_comparison_results.json', 'w') as f:
            json.dump(sweep, f, indent=2, default=str)
        print(f"Sweep comparison saved to sweep_comparison_results.json")


if __name__ == '__main__':
    main()
