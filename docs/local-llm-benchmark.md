# Local LLMs vs Claude Haiku 
## Real-World Ops Tool Calling

|  | GLM-4.7-Flash | Mistral Small | Qwen 3 | Claude Haiku |
|--|---------------|---------------|--------|--------------|
|  | 30B-A3B MoE | 3.2 (24B) | (14B) | (Cloud) |
| Structured tool calling | Yes | Yes | Yes | Yes |
| Tool call format | Native | Native | Native | Native |
| Calls tools without asking permission | Yes | Yes | No (asks first) | Yes |
| Multi-step tool chains | 12+ steps | Good | Slower | Excellent |
| Sweep findings quality | Real issues | Not tested | Adequate | Excellent |
| Speed (tool loop) | Fast | Moderate | Slow | Fast |
| Active parameters | 3B (MoE) | 24B (dense) | 14B (dense) | N/A |
| Runs locally | Yes | Yes | Yes | No |
| API cost | $0 | $0 | $0 | ~$0.01/run |
| Best for | Agent sweeps | cfassist CLI | Code tasks | Fallback |
| **VERDICT** | **Primary** | **CLI tool** | **Backup** | **Safety net** |

## Hardware

| Component | Spec |
|-----------|------|
| GPU | AMD Radeon RX 7900 XTX (24 GB VRAM) |
| CPU | AMD Ryzen 5 7600X 6-Core |
| RAM | 32 GB |
| LLM Runtime | Ollama (ROCm) |
| Agent Host | Raspberry Pi 5 (8 GB) |

**Task:** Autonomous infrastructure monitoring — Prometheus, Loki, Docker, SSH
**Result:** GLM-4.7-Flash runs production. Cloud fallback exists but rarely fires.
