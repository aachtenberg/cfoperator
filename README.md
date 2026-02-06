# CFOperator - Continuous Feedback Operator

An intelligent homelab monitoring agent with dual-mode operation:
- **Reactive**: Responds to alerts with LLM-driven investigations
- **Proactive**: Periodic deep sweeps to catch issues before they alert

Built with learnings from SRE Sentinel, inspired by OODA loop principles.

## Features
- Single central agent (no per-host complexity)
- Chat interface with bidirectional Q&A
- Vector DB memory (pgvector + Ollama embeddings)
- LLM fallback chain (Ollama → Groq → Gemini/Claude)
- Continuous intelligence sweeps
- Learning extraction and consolidation

