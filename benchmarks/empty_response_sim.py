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

Scenarios (CFOP-570): the original healthy-Loki case plus fault cases with an
expected STATUS and the evidence a right answer cites, so a model that always
says "resolved" no longer scores perfectly. --prompt production renders
agent.py's current template, FIX contract included; the default stays the
July skeleton so earlier results remain comparable.

Usage:
    python benchmarks/empty_response_sim.py \
        [--url http://ubuntu-llm-01:11434] \
        [--models gemma4:26b,qwen3.6:27b,glm-4.7-flash:latest] \
        [--trials 10] [--scenarios all] [--prompt production] [--think low] \
        [--output benchmarks/empty_response_sim_results.jsonl]
"""

import argparse
import json
import os
import re
import sys
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

def legacy_prompt(trigger, similar_text):
    """The July 2026 prompt skeleton every committed result was measured on."""
    return f"""You are CFOperator investigating an infrastructure alert.

Alert: {trigger}
Alert details: {{}}
{similar_text}
Investigate this alert using the available tools. Check metrics, logs, and container/service status.
First give a short summary of what you found. Then end your response with exactly these two lines:

STATUS: <one of: resolved | needs_action | monitoring | escalate>
  - resolved: the resource is healthy RIGHT NOW — the problem is gone, or you fixed it during this investigation. Do NOT use resolved just because you identified a fix that someone still has to apply.
  - needs_action: you found the problem but it needs a change you could not make yourself; your RECOMMENDATION says what to do.
  - monitoring: transient or inconclusive; worth watching, no action yet.
  - escalate: urgent; a human should look now.
RECOMMENDATION: <the single most useful operator-facing next step — a concrete command or config change, or "No action needed" when the resource is genuinely healthy>"""


SYSTEM_PROMPT = legacy_prompt(TRIGGER, SIMILAR_TEXT)

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

# ── Fault scenarios (CFOP-570) ────────────────────────────────────────────────
# The healthy Loki scenario cannot tell a model that reads the evidence from
# one that always answers "resolved". Each scenario below has an expected
# STATUS and the evidence a correct answer has to cite. Tools a scenario does
# not mention get its default; the read-only refusal stands in for the
# production guard, so a model that tries to fix things itself learns it
# cannot, as it would live.

READ_ONLY_REFUSAL = {'error': 'refused: this investigation is read-only. '
                              'Put the change you would make in your FIX.'}
MUTATING_TOOLS = ('k8s_rollout_restart', 'ssh_restart_service', 'ssh_docker_restart',
                  'k8s_exec_pod', 'github_create_pr', 'github_create_issue_comment',
                  'store_learning', 'update_sweep_finding')
FAULT_DEFAULT = {'status': 'ok', 'note': 'nothing relevant to this alert in this source'}


def _with_refusals(canned):
    return {**{name: READ_ONLY_REFUSAL for name in MUTATING_TOOLS}, **canned}


OOM_CANNED = _with_refusals({
    'k8s_get_pods': {
        'namespace': 'immich',
        'pods': [
            {'name': 'immich-machine-learning-7d9f8c6b5-q2xkp', 'status': 'CrashLoopBackOff',
             'ready': '0/1', 'restarts': 6, 'age': '2h4m', 'node': 'ubuntu-llm-01'},
            {'name': 'immich-server-5c7b9d8f4-hx7pl', 'status': 'Running', 'ready': '1/1',
             'restarts': 0, 'age': '6d', 'node': 'ubuntu-llm-01'},
            {'name': 'immich-postgres-0', 'status': 'Running', 'ready': '1/1',
             'restarts': 0, 'age': '6d', 'node': 'ubuntu-llm-01'},
        ],
    },
    'k8s_get_pod_status': {
        'pod': 'immich-machine-learning-7d9f8c6b5-q2xkp', 'namespace': 'immich',
        'phase': 'Running', 'ready': False, 'restarts': 6,
        'state': 'waiting: CrashLoopBackOff',
        'last_state': 'terminated: reason=OOMKilled exit_code=137 finished 3m ago',
    },
    'k8s_describe': {
        'kind': 'Pod', 'name': 'immich-machine-learning-7d9f8c6b5-q2xkp', 'namespace': 'immich',
        'containers': [{'name': 'machine-learning',
                        'image': 'ghcr.io/immich-app/immich-machine-learning:v1.135.3',
                        'limits': {'cpu': '2', 'memory': '512Mi'},
                        'requests': {'cpu': '250m', 'memory': '256Mi'},
                        'state': 'waiting (CrashLoopBackOff)',
                        'last_state': 'terminated (OOMKilled, exit 137, 3m ago)'}],
        'events': 'Warning BackOff (x38 over 2h): Back-off restarting failed container',
    },
    'k8s_get_pod_logs': {
        'pod': 'immich-machine-learning-7d9f8c6b5-q2xkp', 'namespace': 'immich',
        'lines': [
            '[10/30/26 12:01:02] INFO  Starting gunicorn 23.0.0',
            '[10/30/26 12:01:09] INFO  Loading clip model "ViT-B-32__openai"',
            '[10/30/26 12:01:15] INFO  Loading facial recognition model "buffalo_l"',
        ],
        'note': 'log ends without an error line; the previous container was killed',
    },
    'k8s_get_events': {
        'namespace': 'immich',
        'events': [
            {'type': 'Warning', 'reason': 'BackOff', 'count': 38,
             'object': 'pod/immich-machine-learning-7d9f8c6b5-q2xkp',
             'message': 'Back-off restarting failed container machine-learning'},
            {'type': 'Warning', 'reason': 'OOMKilling', 'count': 6, 'object': 'node/ubuntu-llm-01',
             'message': 'Memory cgroup out of memory: Killed process 31337 (python3)'},
        ],
    },
    'prometheus_query': {
        'status': 'success',
        'result': [
            {'metric': {'__name__': 'container_memory_working_set_bytes',
                        'pod': 'immich-machine-learning-7d9f8c6b5-q2xkp'},
             'value': [1793366460, '536412160']},
            {'metric': {'__name__': 'kube_pod_container_resource_limits', 'resource': 'memory',
                        'pod': 'immich-machine-learning-7d9f8c6b5-q2xkp'},
             'value': [1793366460, '536870912']},
        ],
        'note': 'peak working set over the last hour vs the container memory limit',
    },
    'loki_query': {
        'streams': [{'labels': '{job="kernel", host="ubuntu-llm-01"}', 'lines': [
            'Memory cgroup out of memory: Killed process 31337 (python3) '
            'total-vm:2861044kB, anon-rss:523904kB, file-rss:9812kB',
            'oom_reaper: reaped process 31337 (python3), now anon-rss:0kB',
        ]}],
    },
    'k8s_get_deployments': {
        'namespace': 'immich',
        'deployments': [{'name': 'immich-machine-learning', 'ready': '0/1',
                         'image': 'ghcr.io/immich-app/immich-machine-learning:v1.135.3',
                         'last_rollout': '6d ago'}],
    },
    'k8s_get_node_metrics': {'nodes': [{'name': 'ubuntu-llm-01', 'cpu_pct': 18,
                                        'memory_pct': 61}]},
})

DISK_CANNED = _with_refusals({
    'prometheus_query': {
        'status': 'success',
        'result': [
            {'metric': {'instance': 'raspberrypi3:9100', 'mountpoint': '/',
                        'device': '/dev/mmcblk0p2', 'expr': 'avail_bytes / size_bytes'},
             'value': [1793366460, '0.031']},
            {'metric': {'instance': 'raspberrypi3:9100', 'mountpoint': '/',
                        'expr': 'predict_linear(avail_bytes[6h], 24h)'},
             'value': [1793366460, '-1.2e9']},
        ],
        'note': 'about 3% free; at the 6h trend the filesystem fills in ~19h',
    },
    'ssh_execute': {
        'host': 'raspberrypi3', 'exit_code': 0,
        'stdout': ('Filesystem      Size  Used Avail Use% Mounted on\n'
                   '/dev/mmcblk0p2   29G   27G  0.9G  97% /\n\n'
                   '6.1G\t/var/log/journal\n'
                   '2.3G\t/var/lib/rancher/k3s/agent/containerd\n'
                   '410M\t/var/log/syslog.1\n\n'
                   'Archived and active journals take up 6.1G in the file system.'),
    },
    'ssh_get_system_info': {'hostname': 'raspberrypi3', 'uptime': '41 days',
                            'disk_root_used_pct': 97, 'memory_used_pct': 48, 'load_1m': 0.6},
    'ssh_get_logs': {'host': 'raspberrypi3', 'unit': 'k3s-agent', 'lines': [
        'eviction_manager.go:366] "Eviction manager: attempting to reclaim" resourceName="ephemeral-storage"',
        'image_gc_manager.go:391] "Failed to garbage collect required amount of images" '
        'attempting to free 2.1GB, but only found 180MB eligible to free',
    ]},
    'k8s_get_nodes': {'nodes': [
        {'name': 'raspberrypi3', 'status': 'Ready',
         'conditions': {'DiskPressure': 'True', 'MemoryPressure': 'False'}},
        {'name': 'raspberrypi', 'status': 'Ready', 'conditions': {'DiskPressure': 'False'}},
    ]},
    'k8s_get_events': {'events': [
        {'type': 'Warning', 'reason': 'EvictionThresholdMet', 'object': 'node/raspberrypi3',
         'message': 'Attempting to reclaim ephemeral-storage'},
        {'type': 'Warning', 'reason': 'FreeDiskSpaceFailed', 'object': 'node/raspberrypi3',
         'message': 'Failed to garbage collect required amount of images'},
    ]},
})

ROLLOUT_ERROR = ('Error: environment variable DATABASE_URL is required but not set\n'
                 '    at loadConfig (/app/dist/config.js:14:11)')

ROLLOUT_CANNED = _with_refusals({
    'k8s_get_pods': {
        'namespace': 'apps',
        'pods': [
            {'name': 'squadmaps-api-6c8d4f7b9-2kq8v', 'status': 'CrashLoopBackOff', 'ready': '0/1',
             'restarts': 9, 'age': '22m', 'node': 'ubuntu-cm5-01'},
            {'name': 'squadmaps-api-6c8d4f7b9-wm5tn', 'status': 'CrashLoopBackOff', 'ready': '0/1',
             'restarts': 9, 'age': '22m', 'node': 'aa-ubuntu-01'},
        ],
    },
    'k8s_get_pod_logs': {'pod': 'squadmaps-api-6c8d4f7b9-2kq8v', 'namespace': 'apps',
                         'lines': [ROLLOUT_ERROR, ROLLOUT_ERROR]},
    'k8s_get_pod_status': {'pod': 'squadmaps-api-6c8d4f7b9-2kq8v', 'phase': 'Running',
                           'ready': False, 'restarts': 9,
                           'last_state': 'terminated: reason=Error exit_code=1'},
    'k8s_get_deployments': {
        'namespace': 'apps',
        'deployments': [{'name': 'squadmaps-api', 'ready': '0/2', 'strategy': 'Recreate',
                         'image': 'ghcr.io/aachtenberg/squadmaps-api:2026.10.30-1',
                         'revision': 14, 'last_rollout': '22m ago',
                         'previous_revision': {'revision': 13,
                                               'image': 'ghcr.io/aachtenberg/squadmaps-api:2026.10.29-3',
                                               'was_healthy_for': '6d'}}],
    },
    'k8s_rollout_status': {'deployment': 'squadmaps-api', 'namespace': 'apps',
                           'message': 'Waiting for deployment "squadmaps-api" rollout to finish: '
                                      '0 of 2 updated replicas are available...'},
    'k8s_describe': {
        'kind': 'Deployment', 'name': 'squadmaps-api', 'namespace': 'apps',
        'env': [{'name': 'DATABASE_HOST', 'from': 'secret/squadmaps-db'},
                {'name': 'DATABASE_PORT', 'value': '5432'},
                {'name': 'DATABASE_NAME', 'value': 'squadmaps'}],
        'events': 'Normal ScalingReplicaSet 22m: Scaled up replica set squadmaps-api-6c8d4f7b9 to 2',
    },
    'k8s_get_events': {'namespace': 'apps', 'events': [
        {'type': 'Warning', 'reason': 'BackOff', 'count': 41,
         'object': 'pod/squadmaps-api-6c8d4f7b9-2kq8v',
         'message': 'Back-off restarting failed container api'},
    ]},
    'github_list_recent_commits': {'repo': 'aachtenberg/squadmaps', 'commits': [
        {'sha': 'a41c9e2', 'age': '31m', 'message': 'Read the database config from DATABASE_URL'},
        {'sha': '7be0d13', 'age': '2d', 'message': 'Tile cache headers'},
    ]},
    'prometheus_query': {'status': 'success', 'result': [
        {'metric': {'__name__': 'kube_deployment_status_replicas_available',
                    'deployment': 'squadmaps-api'}, 'value': [1793366460, '0']}]},
    'loki_query': {'streams': [{'labels': '{namespace="apps", app="squadmaps-api"}',
                                'lines': [ROLLOUT_ERROR]}]},
})

FLAP_CANNED = _with_refusals({
    'k8s_get_nodes': {'nodes': [
        {'name': 'raspberrypi5', 'status': 'Ready', 'ready_since': '14m ago'},
        {'name': 'raspberrypi', 'status': 'Ready'},
        {'name': 'ubuntu-llm-01', 'status': 'Ready'},
    ]},
    'k8s_get_events': {'events': [
        {'type': 'Normal', 'reason': 'NodeNotReady', 'object': 'node/raspberrypi5', 'age': '17m'},
        {'type': 'Normal', 'reason': 'NodeReady', 'object': 'node/raspberrypi5', 'age': '14m'},
    ], 'note': 'no events for raspberrypi5 since'},
    'k8s_get_pods': {'pods': [
        {'name': 'node-exporter-x8f2k', 'status': 'Running', 'ready': '1/1', 'restarts': 0,
         'node': 'raspberrypi5'},
        {'name': 'promtail-q9w4c', 'status': 'Running', 'ready': '1/1', 'restarts': 0,
         'node': 'raspberrypi5'},
    ]},
    'ssh_get_system_info': {'hostname': 'raspberrypi5', 'uptime': '41 days', 'load_1m': 0.4,
                            'interfaces': {'eth0': 'down (unplugged)', 'wlan0': 'up'}},
    'ssh_get_logs': {'host': 'raspberrypi5', 'lines': [
        '12:23:41 wpa_supplicant[611]: wlan0: CTRL-EVENT-DISCONNECTED bssid=3c:52:a1:0e:77:12 reason=4',
        '12:24:02 k3s[903]: "Node became not ready" node="raspberrypi5"',
        '12:26:39 wpa_supplicant[611]: wlan0: CTRL-EVENT-CONNECTED - Connection to 3c:52:a1:0e:77:12 completed',
        '12:26:44 k3s[903]: "Node became ready" node="raspberrypi5"',
    ]},
    'ssh_execute': {'host': 'raspberrypi5', 'exit_code': 0,
                    'stdout': 'wlan0: link ok, signal -61 dBm\n0% packet loss to 192.168.0.1'},
    'prometheus_query': {'status': 'success', 'result': [
        {'metric': {'__name__': 'up', 'instance': 'raspberrypi5:9100'},
         'value': [1793366460, '1']}],
        'note': 'up dropped to 0 for ~3m about 17m ago, 1 since'},
    'ping_host': {'host': 'raspberrypi5', 'reachable': True, 'loss_pct': 0, 'rtt_ms': 4.1},
    'find_learnings': {'learnings': [
        'raspberrypi5 is WiFi-only by design (eth0 unplugged); short NotReady flaps follow '
        'access-point reconnects and clear on their own.']},
})

# expected: the STATUS values a correct answer may give (extract_status names).
# evidence: groups of alternatives; a correct answer mentions one of each group.
SCENARIOS = {
    'loki-healthy': {
        'trigger': TRIGGER, 'alert_info': {}, 'similar_text': SIMILAR_TEXT,
        'canned': CANNED, 'default': DEFAULT_RESULT,
        'expected': {'resolved'}, 'evidence': [['503']],
    },
    'oom-killed': {
        'trigger': ('KubePodCrashLooping: pod immich-machine-learning-7d9f8c6b5-q2xkp in '
                    'namespace immich is restarting'),
        'alert_info': {'alertname': 'KubePodCrashLooping', 'severity': 'warning',
                       'namespace': 'immich', 'pod': 'immich-machine-learning-7d9f8c6b5-q2xkp',
                       'container': 'machine-learning'},
        'similar_text': '', 'canned': OOM_CANNED, 'default': FAULT_DEFAULT,
        'expected': {'needs_action'}, 'evidence': [['oom'], ['512']],
    },
    'disk-full': {
        'trigger': 'NodeFilesystemAlmostOutOfSpace: filesystem / on raspberrypi3 has 3% space left',
        'alert_info': {'alertname': 'NodeFilesystemAlmostOutOfSpace', 'severity': 'critical',
                       'instance': 'raspberrypi3:9100', 'mountpoint': '/',
                       'device': '/dev/mmcblk0p2'},
        'similar_text': '', 'canned': DISK_CANNED, 'default': FAULT_DEFAULT,
        'expected': {'needs_action', 'escalated'},
        'evidence': [['/var/log/journal', '6.1g', '6.1 g']],
    },
    'bad-rollout': {
        'trigger': ('KubeDeploymentReplicasMismatch: deployment squadmaps-api in namespace apps '
                    'has 0/2 available replicas'),
        'alert_info': {'alertname': 'KubeDeploymentReplicasMismatch', 'severity': 'warning',
                       'namespace': 'apps', 'deployment': 'squadmaps-api'},
        'similar_text': '', 'canned': ROLLOUT_CANNED, 'default': FAULT_DEFAULT,
        'expected': {'needs_action'}, 'evidence': [['database_url']],
    },
    'node-flap-recovered': {
        'trigger': 'KubeNodeNotReady: node raspberrypi5 was NotReady for 3m',
        'alert_info': {'alertname': 'KubeNodeNotReady', 'severity': 'warning',
                       'node': 'raspberrypi5'},
        'similar_text': '', 'canned': FLAP_CANNED, 'default': FAULT_DEFAULT,
        'expected': {'monitoring', 'resolved'}, 'evidence': [['wlan', 'wifi', 'wi-fi']],
    },
}


def production_prompt(trigger, alert_info, similar_text):
    """The investigation prompt as agent.py builds it, from its own pieces.

    Mirrors the f-string in CFOperator's investigation path (search agent.py
    for 'You are CFOperator investigating'); the framing, SSH contract, FIX
    schema and alert block are imported, so only the template's glue text can
    drift. Learnings are left out, as in the legacy prompt; delivery guidance
    is given an empty config, which is the shipped 'none' mode.
    """
    _import_agent()
    from agent.agent import (SSH_ACCESS_CONTRACT, _FIX_JSON_SCHEMA, _alert_prompt_block,
                             _delivery_guidance)
    from agent.prompt_injection import frame_untrusted_data, get_system_framing
    return f"""You are CFOperator investigating an infrastructure alert.

Alert trigger: {frame_untrusted_data(trigger, "trigger", 500)}
{_alert_prompt_block(alert_info)}
{similar_text}
{get_system_framing()}

Investigate this alert using the available tools. Check metrics, logs, and container/service status.
{SSH_ACCESS_CONTRACT}
This investigation is read-only: observe, do not change anything. A command that restarts, writes, or changes settings is refused; the change you would make goes in your FIX, and the remediation queue applies it.
First give a short summary of what you found. Then end your response with:

STATUS: <one of: resolved | needs_action | monitoring | escalate>
  - resolved: the resource is healthy RIGHT NOW — the problem is gone. Do NOT use resolved just because you identified a fix that someone still has to apply.
  - needs_action: you found the problem and it needs a change; your RECOMMENDATION says what to do.
  - monitoring: transient or inconclusive; worth watching, no action yet.
  - escalate: urgent; a human should look now.
RECOMMENDATION: <the single most useful operator-facing next step — a concrete command or config change, or "No action needed" when the resource is genuinely healthy>
When STATUS is needs_action, also emit a FIX object after RECOMMENDATION (JSON, not inside the RECOMMENDATION line). targets is a list so a two-repo ordered fix can be stated. Omit FIX only when no change is required.
FIX: {_FIX_JSON_SCHEMA}{_delivery_guidance({})}"""


def _import_agent():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in (os.path.join(root, 'agent'), root):
        if path not in sys.path:
            sys.path.insert(0, path)


TOOL_MARKUP = re.compile(r'<tool_call|<function=|<arg_key>|"tool_calls"|<\|channel\|>', re.I)


def _cites(low, alt):
    """alt at the start of a word: 'oom' is in 'OOMKilled', not in 'room'."""
    return re.search(r'(?<![a-z0-9])' + re.escape(alt), low) is not None


def score_answer(text, scenario):
    """Status, evidence and leaked tool-call markup for one final answer."""
    low = (text or '').lower()
    status = extract_status(text)
    return {
        'final_status': status,
        'status_ok': status in scenario['expected'],
        'cites_evidence': all(any(_cites(low, alt) for alt in group)
                              for group in scenario['evidence']),
        'tool_markup_in_answer': bool(TOOL_MARKUP.search(text or '')),
    }


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


def nudge_retry(url, model, tools, messages, retries, offer_tools, think=None):
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
        if think is not None:
            payload['think'] = think
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


def run_trial(url, model, tools, trial_idx, nudge_retries=0, nudge_with_tools=False,
              scenario_name='loki-healthy', prompt='legacy', think=None):
    scenario = SCENARIOS[scenario_name]
    if prompt == 'production':
        system_prompt = production_prompt(scenario['trigger'], scenario['alert_info'],
                                          scenario['similar_text'])
    else:
        system_prompt = legacy_prompt(scenario['trigger'], scenario['similar_text'])
    canned, default_result = scenario['canned'], scenario['default']
    known_tools = {t['function']['name'] for t in tools}
    messages = [{'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': f"Investigate this alert: {scenario['trigger']}"}]
    tool_calls_count = 0
    called = []
    cache = {}
    rounds = []
    start = time.time()
    final_text = None
    hit_limit = False

    for iteration in range(MAX_ITERATIONS):
        is_final = (iteration == MAX_ITERATIONS - 1)
        payload = {'model': model, 'messages': messages, 'stream': False,
                   'temperature': 0.7}
        if think is not None:
            payload['think'] = think
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
                called.append(name)
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
                    result = canned.get(name, default_result)
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
        'scenario': scenario_name,
        'prompt': prompt,
        'think': think,
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
            url, model, tools, messages, nudge_retries, nudge_with_tools, think)
        result.update({
            'nudge_attempts': attempts,
            'nudge_rounds': nudge_rounds,
            'nudge_recovered': bool(recovered),
            'nudge_response_len': len(recovered),
            'nudge_has_status_line': 'status:' in recovered.lower(),
            'nudge_extracted_status': extract_status(recovered) if recovered else None,
            'nudge_response_head': recovered[:400],
        })

    # Score the answer production would keep: the nudge's, when the first was empty.
    answer = recovered if result.get('nudge_recovered') else ft
    result.update(score_answer(answer, scenario))
    result['invented_tools'] = sorted({n for n in called if n not in known_tools})
    if prompt == 'production' and result['final_status'] == 'needs_action':
        _import_agent()
        from agent.agent import _parse_structured_fix
        result['fix_first_pass'] = _parse_structured_fix(answer) is not None
    result['answer'] = answer or ''

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
    ap.add_argument('--scenarios', default='loki-healthy',
                    help=f"comma list or 'all' (of: {', '.join(SCENARIOS)})")
    ap.add_argument('--prompt', choices=('legacy', 'production'), default='legacy',
                    help="legacy = the July skeleton prior results used; production = "
                         "agent.py's template incl. the FIX contract")
    ap.add_argument('--think', default=None,
                    help="send ollama 'think' (true/false or low/medium/high); unset = "
                         "omit it, as production does")
    args = ap.parse_args()

    with open(args.schemas) as f:
        tools = json.load(f)
    models = [m.strip() for m in args.models.split(',') if m.strip()]
    names = list(SCENARIOS) if args.scenarios == 'all' else \
        [s.strip() for s in args.scenarios.split(',') if s.strip()]
    unknown = [s for s in names if s not in SCENARIOS]
    if unknown:
        ap.error(f"unknown scenario(s): {', '.join(unknown)}")
    think = {'true': True, 'false': False}.get(str(args.think).lower(), args.think)

    all_results = []
    with open(args.output, 'w') as out:
        for model in models:
            for scenario_name in names:
                print(f"\n=== {model} / {scenario_name} ===", flush=True)
                for i in range(args.trials):
                    try:
                        r = run_trial(args.url, model, tools, i + 1,
                                      nudge_retries=args.nudge_retries,
                                      nudge_with_tools=args.nudge_with_tools,
                                      scenario_name=scenario_name, prompt=args.prompt,
                                      think=think)
                    except Exception as e:
                        r = {'model': model, 'scenario': scenario_name, 'trial': i + 1,
                             'error': f"{type(e).__name__}: {e}"}
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
                              f"status_line={r['has_status_line']} -> {r['final_status']} "
                              f"ok={r['status_ok']} evidence={r['cites_evidence']} "
                              f"({r['tool_calls']} calls, {r['llm_rounds']} rounds, "
                              f"{r['duration_s']}s){nudge}", flush=True)

    print("\n=== SUMMARY ===")
    for model in models:
        for scenario_name in names:
            summarize(model, scenario_name,
                      [r for r in all_results
                       if r['model'] == model and r['scenario'] == scenario_name])


def summarize(model, scenario_name, results):
    rs = [r for r in results if 'error' not in r]
    errs = [r for r in results if 'error' in r]
    label = f"{model} / {scenario_name}"
    if not rs:
        print(f"{label}: all {len(errs)} trials errored")
        return
    n = len(rs)
    statuses = {}
    for r in rs:
        statuses[r['final_status']] = statuses.get(r['final_status'], 0) + 1
    usable = sum(1 for r in rs if r['has_status_line'] or r.get('nudge_has_status_line'))
    line = (f"{label}: status ok {sum(r['status_ok'] for r in rs)}/{n} {statuses}, "
            f"usable STATUS {usable}/{n}, "
            f"evidence {sum(r['cites_evidence'] for r in rs)}/{n}, "
            f"tool markup in answer {sum(r['tool_markup_in_answer'] for r in rs)}/{n}, "
            f"invented tools {sorted({t for r in rs for t in r['invented_tools']})}, "
            f"avg {sum(r['duration_s'] for r in rs) / n:.1f}s")
    fixed = [r for r in rs if 'fix_first_pass' in r]
    if fixed:
        line += f", FIX on first pass {sum(r['fix_first_pass'] for r in fixed)}/{len(fixed)}"
    print(line)
    summarize_legacy(model, rs, errs)


def summarize_legacy(model, rs, errs):
    """The pre-CFOP-570 summary line, kept so old and new runs read alike."""
    empty = sum(1 for r in rs if r['empty_response'])
    no_status = sum(1 for r in rs if not r['has_status_line'])
    limit = sum(1 for r in rs if r['hit_iteration_limit'])
    avg_calls = sum(r['tool_calls'] for r in rs) / len(rs)
    line = (f"  {model}: {empty}/{len(rs)} empty responses, "
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
