# Local LLMs vs Claude Haiku 
## Real-World Ops Tool Calling

| Capability | Qwen3-Coder | GLM-4.7-Flash | Mistral Small | Qwen 3 | Claude Haiku |
|--|-------------|---------------|---------------|--------|--------------|
| Size | 30B-A3B MoE | 30B-A3B MoE | 3.2 (24B) | (14B) | (Cloud) |
| Structured tool calling | Yes | Yes | Yes | Yes | Yes |
| Tool call format | Native | Native | Native | Native | Native |
| Calls tools without asking permission | Yes | Yes | Yes | No (asks first) | Yes |
| Multi-step tool chains | Excellent | 12+ steps | Good | Slower | Excellent |
| Sweep findings quality | Strong | Real issues | Not tested | Adequate | Excellent |
| Speed (tool loop) | Very fast | Fast | Moderate | Slow | Fast |
| Throughput (tok/s) | ~91 | ~60 | ~45 | ~60 | N/A |
| Active parameters | 3B (MoE) | 3B (MoE) | 24B (dense) | 14B (dense) | N/A |
| Runs locally | Yes | Yes | Yes | Yes | No |
| API cost | $0 | $0 | $0 | $0 | ~$0.01/run |
| Best for | **Agent sweeps** | Agent sweeps | cfassist CLI | Code tasks | Fallback |
| **VERDICT** | **Primary** | Backup | CLI tool | Backup | Safety net |

## Qwen3-Coder Benchmark Results (2026-04-04)

### Tool-Calling Scores (4 runs)

| Run | T1 Single | T2 Multi-turn | T3 Selection | Score | Time |
|-----|-----------|---------------|--------------|-------|------|
| 1 | 2/2 | 2/2 | 0/2 | 6.7 | 3.3s |
| 2 | 2/2 | 2/2 | 2/2 | **10.0** | 3.0s |
| 3 | 2/2 | 2/2 | 2/2 | **10.0** | 3.1s |
| 4 | 2/2 | 1/2 | 2/2 | 8.3 | 2.7s |

**Average: 8.8/10** — Scores 10.0 on 50% of runs. Occasional multi-turn or tool-selection miss, similar to GLM-4.7-Flash and mistral-small3.2 variance pattern. Fastest response time of any model tested (2.7–3.3s vs 6.1s for mistral-small3.2).

### Inference Latency (5 iterations per prompt)

| Category | Avg TTFT (s) | Avg Total (s) | Avg TPS |
|----------|-------------|---------------|---------|
| triage-short | 9.1 | 12.6 | 66.1 |
| analysis-medium | 3.5 | 8.6 | 83.1 |
| reasoning-long | 2.9 | 8.4 | 77.5 |

**vs Qwen3:14b (previous primary):**

| Metric | Qwen3-Coder | Qwen3:14b | Improvement |
|--------|------------|-----------|-------------|
| Triage TTFT | 9.1s | 9.9s | 8% faster |
| Analysis total | 8.6s | 16.0s | **46% faster** |
| Reasoning total | 8.4s | 27.8s | **70% faster** |
| Peak throughput | 92 tok/s | 60 tok/s | **53% higher** |

Qwen3-Coder is significantly faster at analysis and reasoning tasks while maintaining similar triage performance. Throughput is 53% higher thanks to the MoE architecture activating only 3B params per token.

## Hardware

| Component | Spec |
|-----------|------|
| GPU | AMD Radeon RX 7900 XTX (24 GB VRAM) |
| CPU | AMD Ryzen 5 7600X 6-Core |
| RAM | 32 GB |
| LLM Runtime | Ollama (ROCm) |
| Agent Host | headless-gpu (k3s node) |

**Task:** Autonomous infrastructure monitoring — Prometheus, Loki, Kubernetes, Docker, SSH
**Result:** Qwen3-Coder replaces GLM-4.7-Flash as primary production model. Cloud fallback exists but rarely fires.
