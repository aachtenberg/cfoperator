FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    iputils-ping \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install kubectl for K8s tools
RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
# Config semantics shared by agent/ and event_runtime/. Both import it at module
# load to resolve config.yaml, so omitting it is an ImportError at startup for
# the agent, the MCP pod and the event runtime alike — not a degraded config.
COPY cfshared/ ./cfshared/
COPY agent/ ./agent/
COPY web_server.py ./
# web_server.py imports this at module load, so a missing copy is not a
# degraded console — it is an ImportError that crash-loops the whole agent.
COPY web_auth.py ./
# Same rule, same reason: web_server.py and mcp_server/server.py both import
# auth.bootstrap at module load. Leaving it out crash-looped the agent and the
# MCP pod on the deploy that introduced them, with :8083 refusing connections.
COPY auth/ ./auth/
# docs/auth.md's lockout runbook is `kubectl exec ... python
# scripts/create_admin.py`, which is the recovery path for having no usable
# admin — it has to exist in the image to be worth documenting.
COPY scripts/ ./scripts/
COPY observability/ ./observability/
COPY tools/ ./tools/
COPY skills/ ./skills/
COPY ui/ ./ui/
COPY event_runtime/ ./event_runtime/
# MCP facade — the sibling MCP Deployment reuses this image with
# command: ["python", "-m", "mcp_server"]
COPY mcp_server/ ./mcp_server/
# Slack bridge — sibling Deployment, command: ["python", "-m", "bridge"]
COPY bridge/ ./bridge/
COPY config.yaml.example ./config.yaml.example

# agent.py uses bare imports (knowledge_base, llm_fallback, etc.)
ENV PYTHONPATH="/app/agent:/app:${PYTHONPATH}"

# Expose port for HTTP/WebSocket
EXPOSE 8083

# Run the agent
CMD ["python", "-m", "agent"]
