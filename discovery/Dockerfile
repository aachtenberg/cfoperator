# cfoperator-discovery: one-shot fleet characterization image (CFOP-47).
#
# Deterministic enumeration (Prometheus + optional k8s API) + a bounded LLM
# characterization pass. Report-only with just PROMETHEUS_URL + an LLM backend;
# adds nothing but a POST to the agent's /api/learnings when CFOP_AGENT_URL +
# CFOP_API_TOKEN are set. Read-only toward the infrastructure; no SSH — note
# the deliberate absence of openssh-client here, unlike the executor image.
# Stdlib-only at runtime (urllib), so the image is ~base python-slim.

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 discovery
USER discovery
WORKDIR /home/discovery

COPY --chown=discovery:discovery llm.py /home/discovery/llm.py
COPY --chown=discovery:discovery inventory.py /home/discovery/inventory.py
COPY --chown=discovery:discovery entrypoint.py /home/discovery/entrypoint.py

ENTRYPOINT ["python3", "/home/discovery/entrypoint.py"]
