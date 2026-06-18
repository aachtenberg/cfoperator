# cfoperator-executor: ephemeral remediation Job image.
#
# Portable, model-swappable diff producer. Reads a work order from env, renders
# templates/remediation.md, calls the configured LLM backend (anthropic /
# openai-compatible / claude-cli — CFOP_EXEC_LLM_BACKEND), extracts a unified
# diff, opens a PR against the GitOps repo, and POSTs the outcome to the agent.
#
# Read-only toward the cluster (the Job's service account has no write verbs);
# the only mutation is a GitHub PR a human merges and ArgoCD then syncs.
#
# Multi-arch (amd64 + arm64): Jobs schedule on any cluster node.

FROM node:22-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# kubectl for read-only cluster inspection (no write verbs on the SA).
ARG TARGETARCH
RUN curl -fsSLo /usr/local/bin/kubectl \
    "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/${TARGETARCH}/kubectl" \
    && chmod +x /usr/local/bin/kubectl

# claude CLI only needed for the claude-cli backend; HTTP backends use stdlib.
RUN npm install -g @anthropic-ai/claude-code

RUN useradd --create-home --uid 10001 executor
USER executor
WORKDIR /home/executor

COPY --chown=executor:executor diff.py /home/executor/diff.py
COPY --chown=executor:executor llm.py /home/executor/llm.py
COPY --chown=executor:executor github.py /home/executor/github.py
COPY --chown=executor:executor entrypoint.py /home/executor/entrypoint.py
COPY --chown=executor:executor templates/ /home/executor/templates/

ENTRYPOINT ["python3", "/home/executor/entrypoint.py"]
