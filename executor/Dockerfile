# cfoperator-executor: ephemeral remediation Job image.
#
# Portable, model-swappable diff producer. Reads a work order from env, renders
# templates/remediation.md, calls the configured LLM backend (anthropic /
# openai-compatible over HTTP — CFOP_EXEC_LLM_BACKEND), extracts a unified diff,
# opens a PR against the target GitOps repo, and POSTs the outcome to the agent.
#
# Stdlib-only at runtime (urllib): the default HTTP backends need no Node, no
# Claude CLI, and no kubectl — so this stays tiny (~base python-slim) and pulls
# fast on arm64 nodes. (The claude-cli backend would need a fuller image with
# the CLI + kubectl; build a separate variant if you want it.)
#
# Read-only toward the cluster (its SA has no write verbs). GitOps classes only
# mutate via a GitHub PR a human merges and ArgoCD then syncs; the node-action
# class additionally SSHes to a host to run a gated command plan (opt-in via
# CFOP_NODE_ACTION_ENABLED). Multi-arch (amd64 + arm64).

FROM python:3.12-slim

# ca-certificates for HTTPS (api.anthropic.com / api.github.com via urllib);
# openssh-client for the node-action path (guarded SSH command plans).
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 executor
USER executor
WORKDIR /home/executor

COPY --chown=executor:executor diff.py /home/executor/diff.py
COPY --chown=executor:executor llm.py /home/executor/llm.py
COPY --chown=executor:executor github.py /home/executor/github.py
COPY --chown=executor:executor change_record_client.py /home/executor/change_record_client.py
COPY --chown=executor:executor nodeaction.py /home/executor/nodeaction.py
COPY --chown=executor:executor entrypoint.py /home/executor/entrypoint.py
COPY --chown=executor:executor templates/ /home/executor/templates/

ENTRYPOINT ["python3", "/home/executor/entrypoint.py"]
