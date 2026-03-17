#!/usr/bin/env python3
"""
Ollama Inference Latency Benchmark for CFOperator / Qwen3:14b

Sends representative prompts (matching real CFOperator sweep workloads) to the
Ollama API, measures TTFT, total latency, throughput, and captures GPU stats.

Usage:
    python benchmarks/ollama_latency_bench.py [--url http://localhost:11434] \
                                               [--model qwen3:14b] \
                                               [--iterations 10] \
                                               [--output benchmarks/results.md]
"""

import argparse
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError

# ── Representative prompts modelled on real CFOperator sweep phases ───────────

PROMPTS = [
    # ── Short / triage prompts ────────────────────────────────────────────
    {
        "category": "triage-short",
        "label": "Alert severity classification",
        "messages": [
            {"role": "system", "content": "You are CFOperator, an autonomous SRE agent."},
            {"role": "user", "content": (
                "Alertmanager fired: KubePodCrashLooping for pod "
                "ingress-nginx-controller-7f4d9b6c8-x2k9m in namespace ingress-nginx. "
                "Restarts: 14 in the last hour. Is this critical?"
            )},
        ],
    },
    {
        "category": "triage-short",
        "label": "Quick health check response",
        "messages": [
            {"role": "system", "content": "You are CFOperator, an autonomous SRE agent."},
            {"role": "user", "content": (
                "Prometheus query node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes "
                "returned 0.04 for host raspberrypi3. Is this concerning?"
            )},
        ],
    },
    {
        "category": "triage-short",
        "label": "Simple remediation suggestion",
        "messages": [
            {"role": "system", "content": "You are CFOperator, an autonomous SRE agent."},
            {"role": "user", "content": (
                "Pod coredns-5d78c9869d-abc12 is in ImagePullBackOff state. "
                "The image is rancher/mirrored-coredns-coredns:1.10.1. "
                "What should I check first?"
            )},
        ],
    },
    # ── Medium / analysis prompts ─────────────────────────────────────────
    {
        "category": "analysis-medium",
        "label": "Metrics sweep analysis",
        "messages": [
            {"role": "system", "content": (
                "You are CFOperator performing a proactive infrastructure sweep.\n"
                "After investigating, respond with your findings as a JSON array:\n"
                '[{"severity": "info|warning|critical", "finding": "description", '
                '"evidence": "exact tool output", "remediation": "suggested fix"}]\n'
                "If everything looks healthy, return an empty array: []"
            )},
            {"role": "user", "content": (
                "Here are the current Prometheus metrics for the k3s cluster:\n\n"
                "node_cpu_seconds_total{mode='idle'} shows 12% idle on node gaming1\n"
                "node_filesystem_avail_bytes / node_filesystem_size_bytes = 0.08 on /dev/sda1 (gaming1)\n"
                "kubelet_running_pods = 47 on gaming1, 23 on gaming2, 12 on raspberrypi3\n"
                "node_memory_MemAvailable_bytes = 1.2GB on gaming1 (total 32GB)\n"
                "up{job='node-exporter'} = 1 for all hosts\n"
                "kube_pod_status_phase{phase='Running'} = 78, Pending = 2, Failed = 1\n\n"
                "Analyze these metrics and report any findings."
            )},
        ],
    },
    {
        "category": "analysis-medium",
        "label": "Log pattern analysis",
        "messages": [
            {"role": "system", "content": (
                "You are CFOperator performing a log sweep.\n"
                "Analyze the following log lines and identify errors, warnings, or "
                "concerning patterns. Return findings as JSON."
            )},
            {"role": "user", "content": (
                "Recent Loki query results for {namespace='kube-system'}:\n\n"
                "2026-03-17T08:12:01Z coredns E0317 08:12:01.234 plugin/errors: 2 errors\n"
                "2026-03-17T08:12:05Z coredns E0317 08:12:05.891 plugin/errors: 2 errors\n"
                "2026-03-17T08:13:12Z flannel I0317 08:13:12.001 Lease renewed\n"
                "2026-03-17T08:14:22Z k3s W0317 08:14:22.445 Slow response: GET /api/v1/nodes 1.8s\n"
                "2026-03-17T08:14:30Z k3s W0317 08:14:30.102 Slow response: LIST /api/v1/pods 2.1s\n"
                "2026-03-17T08:15:01Z traefik level=error msg=\"service not available\" service=my-app\n"
                "2026-03-17T08:15:03Z traefik level=error msg=\"service not available\" service=my-app\n"
                "2026-03-17T08:15:05Z traefik level=error msg=\"service not available\" service=my-app\n"
                "2026-03-17T08:16:00Z local-path-provisioner I0317 Provisioned volume pvc-abc123\n"
                "2026-03-17T08:17:44Z k3s E0317 etcd: request timed out\n"
            )},
        ],
    },
    # ── Long / reasoning prompts ──────────────────────────────────────────
    {
        "category": "reasoning-long",
        "label": "Correlation analysis",
        "messages": [
            {"role": "system", "content": (
                "You are CFOperator. Analyze operational data and identify patterns, "
                "root causes, or concerns.\n"
                'Return ONLY valid JSON:\n'
                '{"insights": [{"learning_type": "pattern|solution|root_cause|antipattern|insight", '
                '"title": "...", "description": "...", "services": [...], '
                '"category": "resource|network|config|dependency"}]}'
            )},
            {"role": "user", "content": (
                "SWEEP FINDINGS (last 3 cycles):\n"
                "Cycle 1: [{\"severity\":\"warning\",\"finding\":\"gaming1 disk at 92%\","
                "\"evidence\":\"node_filesystem_avail=0.08\",\"remediation\":\"clean /var/log\"},"
                "{\"severity\":\"info\",\"finding\":\"coredns restarts=3\","
                "\"evidence\":\"kube_pod_container_status_restarts_total=3\","
                "\"remediation\":\"check memory limits\"}]\n"
                "Cycle 2: [{\"severity\":\"warning\",\"finding\":\"gaming1 disk at 93%\","
                "\"evidence\":\"node_filesystem_avail=0.07\",\"remediation\":\"clean /var/log\"},"
                "{\"severity\":\"critical\",\"finding\":\"etcd latency >500ms\","
                "\"evidence\":\"etcd_request_duration_seconds p99=0.8\","
                "\"remediation\":\"check disk I/O\"}]\n"
                "Cycle 3: [{\"severity\":\"critical\",\"finding\":\"gaming1 disk at 95%\","
                "\"evidence\":\"node_filesystem_avail=0.05\",\"remediation\":\"immediate cleanup\"},"
                "{\"severity\":\"critical\",\"finding\":\"etcd leader changes=4\","
                "\"evidence\":\"etcd_server_leader_changes_seen_total=4\","
                "\"remediation\":\"investigate disk pressure\"},"
                "{\"severity\":\"warning\",\"finding\":\"5 pods evicted from gaming1\","
                "\"evidence\":\"kube_pod_status_reason{reason=Evicted}=5\","
                "\"remediation\":\"check DiskPressure taint\"}]\n\n"
                "SERVICE FAILURE PATTERNS (7-day window):\n"
                "- gaming1 node_exporter: 2 scrape failures (Mar 15, Mar 16)\n"
                "- etcd: latency spikes correlate with gaming1 disk >90%\n"
                "- coredns: intermittent restarts on raspberrypi3 (3x this week)\n\n"
                "OPERATIONAL SUMMARY:\n"
                "- Sweeps: 144 total, avg 2.3 findings/sweep\n"
                "- Escalations: 3 critical in last 24h\n"
                "- Remediation success rate: 67%\n\n"
                "Identify patterns, root causes, and recommended actions."
            )},
        ],
    },
    {
        "category": "reasoning-long",
        "label": "Container sweep + tool-call reasoning",
        "messages": [
            {"role": "system", "content": (
                "You are CFOperator performing a container health sweep across the fleet.\n"
                "Review workload health across Kubernetes pods, bare-metal services, "
                "and Docker containers. Check restart counts, resource limits, OOM kills, "
                "and scheduling issues.\n"
                "After investigating, respond with findings as a JSON array:\n"
                '[{"severity": "info|warning|critical", "finding": "description", '
                '"evidence": "exact tool output", "remediation": "suggested fix"}]'
            )},
            {"role": "user", "content": (
                "Kubernetes pod status across all namespaces:\n\n"
                "NAMESPACE       NAME                                    READY  STATUS          RESTARTS  AGE\n"
                "kube-system     coredns-5d78c9869d-abc12                1/1    Running         3         5d\n"
                "kube-system     local-path-provisioner-6c86858495-def34 1/1    Running         0         12d\n"
                "kube-system     traefik-7d5f6474df-ghi56                1/1    Running         1         5d\n"
                "monitoring      prometheus-0                            1/1    Running         0         12d\n"
                "monitoring      loki-0                                  1/1    Running         0         12d\n"
                "monitoring      grafana-6f8b9c7d4f-jkl78                1/1    Running         0         12d\n"
                "cfoperator      cfoperator-agent-5f4d3c2b1a-mno90       1/1    Running         0         2d\n"
                "cfoperator      ollama-llm01-0                          1/1    Running         0         5d\n"
                "cfoperator      ollama-gaming1-0                        1/1    Running         0         5d\n"
                "cfoperator      ollama-gaming2-0                        1/1    Running         0         5d\n"
                "ingress-nginx   ingress-nginx-controller-7f4d9b-x2k9m  0/1    CrashLoopBack   14        1d\n"
                "default         my-app-deployment-8a7b6c5d4e-pqr12     1/1    Running         0         3d\n"
                "default         my-app-deployment-8a7b6c5d4e-stu34     0/1    Pending         0         3d\n"
                "default         redis-master-0                          1/1    Running         0         12d\n\n"
                "Docker containers on bare-metal host 'homelab-nas':\n"
                "CONTAINER ID  IMAGE                    STATUS          NAMES\n"
                "a1b2c3d4e5f6  linuxserver/plex:latest  Up 12 days      plex\n"
                "f6e5d4c3b2a1  linuxserver/sonarr       Up 12 days      sonarr\n"
                "1a2b3c4d5e6f  minio/minio              Up 5 days       minio\n"
                "6f5e4d3c2b1a  postgres:15              Exited (137)    postgres-backup\n\n"
                "Resource usage (top pods by memory):\n"
                "ollama-gaming1-0:    MEM 28.1Gi / 32Gi  CPU 7.2 / 8\n"
                "ollama-llm01-0:      MEM 14.2Gi / 16Gi  CPU 3.1 / 4\n"
                "prometheus-0:        MEM 2.1Gi / 4Gi    CPU 0.3 / 2\n"
                "loki-0:              MEM 1.8Gi / 4Gi    CPU 0.2 / 2\n"
                "cfoperator-agent:    MEM 0.8Gi / 2Gi    CPU 0.4 / 2\n\n"
                "OOMKilled events in last 24h:\n"
                "- coredns-5d78c9869d-abc12 killed 2x (limit: 170Mi, peak: 185Mi)\n\n"
                "Analyze all workloads and report findings."
            )},
        ],
    },
]


def parse_args():
    p = argparse.ArgumentParser(description="Ollama latency benchmark for CFOperator")
    p.add_argument("--url", default="http://localhost:11434", help="Ollama base URL")
    p.add_argument("--model", default="qwen3:14b", help="Model name")
    p.add_argument("--iterations", type=int, default=10, help="Runs per prompt")
    p.add_argument("--output", default="benchmarks/results.md", help="Markdown output path")
    p.add_argument("--warmup", type=int, default=1, help="Warmup runs per prompt (discarded)")
    return p.parse_args()


def gpu_snapshot():
    """Capture GPU utilisation and memory via nvidia-smi."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=index,name,utilization.gpu,utilization.memory,"
             "memory.used,memory.total,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"],
            text=True, timeout=5,
        )
        gpus = []
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 8:
                gpus.append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "gpu_util_pct": float(parts[2]),
                    "mem_util_pct": float(parts[3]),
                    "mem_used_mib": float(parts[4]),
                    "mem_total_mib": float(parts[5]),
                    "temp_c": float(parts[6]),
                    "power_w": float(parts[7]) if parts[7] != "[N/A]" else None,
                })
        return gpus
    except (FileNotFoundError, subprocess.SubprocessError):
        return None


def ollama_chat_streaming(url: str, model: str, messages: list[dict]):
    """
    Call Ollama /api/chat with stream=true.
    Returns (ttft_s, total_s, tokens_generated, full_response).
    """
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {"temperature": 0.7},
    }).encode()

    req = Request(f"{url}/api/chat", data=payload,
                  headers={"Content-Type": "application/json"})

    t_start = time.perf_counter()
    ttft = None
    tokens = 0
    response_text = ""

    with urlopen(req, timeout=300) as resp:
        for raw_line in resp:
            chunk = json.loads(raw_line)
            if chunk.get("message", {}).get("content"):
                if ttft is None:
                    ttft = time.perf_counter() - t_start
                response_text += chunk["message"]["content"]
            if chunk.get("done"):
                tokens = chunk.get("eval_count", 0)
                break

    total = time.perf_counter() - t_start
    if ttft is None:
        ttft = total
    return ttft, total, tokens, response_text


def percentile(data, pct):
    """Simple percentile without numpy."""
    s = sorted(data)
    k = (len(s) - 1) * pct / 100
    f = int(k)
    c = f + 1
    if c >= len(s):
        return s[f]
    return s[f] + (k - f) * (s[c] - s[f])


def stats_summary(values):
    if not values:
        return {"min": 0, "max": 0, "mean": 0, "median": 0, "p95": 0}
    return {
        "min": min(values),
        "max": max(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "p95": percentile(values, 95),
    }


def check_model_available(url, model):
    """Verify the model is loaded in Ollama."""
    try:
        resp = urlopen(f"{url}/api/tags", timeout=10)
        data = json.loads(resp.read())
        names = [m["name"] for m in data.get("models", [])]
        # Match with or without :latest suffix
        for n in names:
            if n == model or n.startswith(model.split(":")[0]):
                return True, names
        return False, names
    except Exception as e:
        return False, [str(e)]


def run_benchmark(args):
    print(f"Ollama Latency Benchmark — {args.model} @ {args.url}")
    print(f"Iterations: {args.iterations}  |  Warmup: {args.warmup}")
    print("=" * 70)

    # Pre-flight
    available, models = check_model_available(args.url, args.model)
    if not available:
        print(f"\n✗ Model '{args.model}' not found. Available: {models}")
        sys.exit(1)
    print(f"✓ Model found. Available models: {models}\n")

    # GPU baseline
    gpu_before = gpu_snapshot()

    all_results = []
    gpu_during = []

    for idx, prompt in enumerate(PROMPTS):
        label = prompt["label"]
        cat = prompt["category"]
        print(f"[{idx+1}/{len(PROMPTS)}] {cat}: {label}")

        # Warmup (discarded)
        for w in range(args.warmup):
            try:
                ollama_chat_streaming(args.url, args.model, prompt["messages"])
                print(f"  warmup {w+1}/{args.warmup} done")
            except Exception as e:
                print(f"  warmup {w+1} failed: {e}")

        ttfts, totals, token_counts, tps_list = [], [], [], []

        for i in range(args.iterations):
            try:
                ttft, total, tokens, _ = ollama_chat_streaming(
                    args.url, args.model, prompt["messages"]
                )
                ttfts.append(ttft)
                totals.append(total)
                token_counts.append(tokens)
                tps = tokens / total if total > 0 else 0
                tps_list.append(tps)
                print(f"  iter {i+1:2d}/{args.iterations}: "
                      f"TTFT={ttft:.3f}s  total={total:.2f}s  "
                      f"tokens={tokens}  tps={tps:.1f}")
            except Exception as e:
                print(f"  iter {i+1:2d}/{args.iterations}: ERROR - {e}")

            # Sample GPU mid-inference
            snap = gpu_snapshot()
            if snap:
                gpu_during.append(snap)

        result = {
            "category": cat,
            "label": label,
            "ttft": stats_summary(ttfts),
            "total": stats_summary(totals),
            "tokens": stats_summary(token_counts),
            "tps": stats_summary(tps_list),
            "runs": len(ttfts),
        }
        all_results.append(result)
        print(f"  → mean TTFT={result['ttft']['mean']:.3f}s  "
              f"mean total={result['total']['mean']:.2f}s  "
              f"mean tps={result['tps']['mean']:.1f}\n")

    # GPU after
    gpu_after = gpu_snapshot()

    # Build markdown report
    report = build_report(args, all_results, gpu_before, gpu_during, gpu_after)
    with open(args.output, "w") as f:
        f.write(report)
    print(f"\n✓ Report written to {args.output}")

    # Also print the report
    print("\n" + report)


def fmt(v, decimals=2):
    if isinstance(v, int):
        return str(v)
    return f"{v:.{decimals}f}"


def build_report(args, results, gpu_before, gpu_during, gpu_after):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Ollama Inference Latency Benchmark",
        f"",
        f"**Model:** `{args.model}` | **Endpoint:** `{args.url}` | "
        f"**Iterations:** {args.iterations} | **Date:** {ts}",
        "",
        "---",
        "",
        "## Results by Prompt",
        "",
        "### Time to First Token (TTFT) — seconds",
        "",
        "| Prompt | Category | Min | Mean | Median | P95 | Max |",
        "|--------|----------|-----|------|--------|-----|-----|",
    ]
    for r in results:
        s = r["ttft"]
        lines.append(
            f"| {r['label']} | {r['category']} | "
            f"{fmt(s['min'],3)} | {fmt(s['mean'],3)} | {fmt(s['median'],3)} | "
            f"{fmt(s['p95'],3)} | {fmt(s['max'],3)} |"
        )

    lines += [
        "",
        "### Total Response Time — seconds",
        "",
        "| Prompt | Category | Min | Mean | Median | P95 | Max |",
        "|--------|----------|-----|------|--------|-----|-----|",
    ]
    for r in results:
        s = r["total"]
        lines.append(
            f"| {r['label']} | {r['category']} | "
            f"{fmt(s['min'])} | {fmt(s['mean'])} | {fmt(s['median'])} | "
            f"{fmt(s['p95'])} | {fmt(s['max'])} |"
        )

    lines += [
        "",
        "### Tokens Generated",
        "",
        "| Prompt | Category | Min | Mean | Median | P95 | Max |",
        "|--------|----------|-----|------|--------|-----|-----|",
    ]
    for r in results:
        s = r["tokens"]
        lines.append(
            f"| {r['label']} | {r['category']} | "
            f"{fmt(s['min'],0)} | {fmt(s['mean'],1)} | {fmt(s['median'],1)} | "
            f"{fmt(s['p95'],1)} | {fmt(s['max'],0)} |"
        )

    lines += [
        "",
        "### Tokens per Second (throughput)",
        "",
        "| Prompt | Category | Min | Mean | Median | P95 | Max |",
        "|--------|----------|-----|------|--------|-----|-----|",
    ]
    for r in results:
        s = r["tps"]
        lines.append(
            f"| {r['label']} | {r['category']} | "
            f"{fmt(s['min'],1)} | {fmt(s['mean'],1)} | {fmt(s['median'],1)} | "
            f"{fmt(s['p95'],1)} | {fmt(s['max'],1)} |"
        )

    # Aggregate by category
    lines += ["", "---", "", "## Aggregate by Category", ""]
    cats = {}
    for r in results:
        c = r["category"]
        if c not in cats:
            cats[c] = {"ttfts": [], "totals": [], "tps": []}
        cats[c]["ttfts"].append(r["ttft"]["mean"])
        cats[c]["totals"].append(r["total"]["mean"])
        cats[c]["tps"].append(r["tps"]["mean"])

    lines += [
        "| Category | Avg TTFT (s) | Avg Total (s) | Avg TPS |",
        "|----------|-------------|---------------|---------|",
    ]
    for cat, v in cats.items():
        lines.append(
            f"| {cat} | {fmt(statistics.mean(v['ttfts']),3)} | "
            f"{fmt(statistics.mean(v['totals']))} | "
            f"{fmt(statistics.mean(v['tps']),1)} |"
        )

    # GPU section
    lines += ["", "---", "", "## GPU Utilization", ""]
    if gpu_before:
        lines += ["### Baseline (pre-benchmark)", ""]
        lines += [
            "| GPU | Name | GPU% | Mem% | VRAM Used | VRAM Total | Temp °C | Power W |",
            "|-----|------|------|------|-----------|------------|---------|---------|",
        ]
        for g in gpu_before:
            pw = fmt(g["power_w"], 1) if g["power_w"] is not None else "N/A"
            lines.append(
                f"| {g['index']} | {g['name']} | {g['gpu_util_pct']:.0f} | "
                f"{g['mem_util_pct']:.0f} | {g['mem_used_mib']:.0f} MiB | "
                f"{g['mem_total_mib']:.0f} MiB | {g['temp_c']:.0f} | {pw} |"
            )

    if gpu_during:
        # Aggregate peak stats across all samples
        all_gpus = {}
        for snap in gpu_during:
            for g in snap:
                idx = g["index"]
                if idx not in all_gpus:
                    all_gpus[idx] = {"name": g["name"], "gpu_util": [],
                                     "mem_used": [], "temp": [], "power": []}
                all_gpus[idx]["gpu_util"].append(g["gpu_util_pct"])
                all_gpus[idx]["mem_used"].append(g["mem_used_mib"])
                all_gpus[idx]["temp"].append(g["temp_c"])
                if g["power_w"] is not None:
                    all_gpus[idx]["power"].append(g["power_w"])

        lines += ["", "### During Inference (sampled per iteration)", ""]
        lines += [
            "| GPU | Name | GPU% mean/peak | VRAM mean/peak MiB | Temp mean/peak °C | Power mean/peak W |",
            "|-----|------|----------------|--------------------|--------------------|-------------------|",
        ]
        for idx in sorted(all_gpus):
            g = all_gpus[idx]
            gm, gp = statistics.mean(g["gpu_util"]), max(g["gpu_util"])
            mm, mp = statistics.mean(g["mem_used"]), max(g["mem_used"])
            tm, tp = statistics.mean(g["temp"]), max(g["temp"])
            if g["power"]:
                pm, pp = statistics.mean(g["power"]), max(g["power"])
                pw = f"{pm:.0f} / {pp:.0f}"
            else:
                pw = "N/A"
            lines.append(
                f"| {idx} | {g['name']} | {gm:.0f} / {gp:.0f} | "
                f"{mm:.0f} / {mp:.0f} | {tm:.0f} / {tp:.0f} | {pw} |"
            )

    if gpu_after:
        lines += ["", "### Post-benchmark", ""]
        lines += [
            "| GPU | Name | GPU% | Mem% | VRAM Used | Temp °C |",
            "|-----|------|------|------|-----------|---------|",
        ]
        for g in gpu_after:
            lines.append(
                f"| {g['index']} | {g['name']} | {g['gpu_util_pct']:.0f} | "
                f"{g['mem_util_pct']:.0f} | {g['mem_used_mib']:.0f} MiB | {g['temp_c']:.0f} |"
            )

    if not gpu_before and not gpu_during:
        lines.append("*nvidia-smi not available — GPU stats could not be captured.*")

    lines += [
        "", "---", "",
        "*Generated by `benchmarks/ollama_latency_bench.py` — CFOperator project*",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    run_benchmark(parse_args())
