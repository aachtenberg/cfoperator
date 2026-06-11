# cfoperator-worker: ephemeral deep-investigation Job image.
# Headless Claude Code + ssh + read-only kubectl. Launched by the
# event_runtime DeepInvestigationActionHandler; entrypoint.py renders the
# prompt template, runs `claude -p`, and posts the report back to the
# event runtime's completion endpoint.
#
# Multi-arch (amd64 + arm64): Jobs schedule on any cluster node, including
# the Raspberry Pis.

FROM node:22-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    openssh-client \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# kubectl for read-only cluster inspection (the Job's service account has no
# write verbs, so this can't mutate anything even if misused).
ARG TARGETARCH
RUN curl -fsSLo /usr/local/bin/kubectl \
    "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/${TARGETARCH}/kubectl" \
    && chmod +x /usr/local/bin/kubectl

RUN npm install -g @anthropic-ai/claude-code

# Non-root. The SSH key secret is mounted at /ssh-secret (root:fsGroup,
# group-readable); entrypoint.py copies it into ~/.ssh with 0600 because the
# ssh client refuses group-readable private keys.
RUN useradd --create-home --uid 10001 worker
USER worker
WORKDIR /home/worker

COPY --chown=worker:worker entrypoint.py /home/worker/entrypoint.py
COPY --chown=worker:worker templates/ /home/worker/templates/

ENTRYPOINT ["python3", "/home/worker/entrypoint.py"]
