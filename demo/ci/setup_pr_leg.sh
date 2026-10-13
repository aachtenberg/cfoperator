#!/usr/bin/env bash
# CFOP-315: prepare the PR leg of the demo e2e, before demo/up.sh runs.
#
#   demo/ci/setup_pr_leg.sh [llm-port] [github-port] >> "$GITHUB_ENV"
#
# - creates the kind cluster (up.sh reuses it), because the stubs' address is
#   kind's docker network gateway and that network only exists afterwards;
# - builds the executor image from THIS checkout and `kind load`s it, since the
#   e2e config sets image_pull_policy: Never. Testing a pulled :main would
#   test the last release, not the change;
# - prints the env the rest of the job needs, as KEY=VALUE lines.
#
# The stubs themselves (llm_stub.py, github_stub.py) are started by the caller
# and must listen on 0.0.0.0, so the gateway address reaches them.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

LLM_PORT="${1:-11434}"
GITHUB_PORT="${2:-9300}"
CLUSTER=cfop-demo
IMAGE=cfoperator-executor:e2e

if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  kind create cluster --name "$CLUSTER" --wait 120s >&2
fi

GATEWAY=$(docker network inspect kind \
  --format '{{range .IPAM.Config}}{{.Gateway}} {{end}}' | tr ' ' '\n' | grep -E '^[0-9.]+$' | head -1)
[[ -n "$GATEWAY" ]] || { echo "no IPv4 gateway on the kind network" >&2; exit 1; }

docker build -q -t "$IMAGE" executor/ >&2
kind load docker-image "$IMAGE" --name "$CLUSTER" >&2

echo "CFOP_E2E_GITHUB_URL=http://${GATEWAY}:${GITHUB_PORT}"
echo "CFOP_E2E_LLM_URL=http://${GATEWAY}:${LLM_PORT}/v1"
echo "CFOP_E2E_GITHUB_STUB=http://127.0.0.1:${GITHUB_PORT}"
echo "CFOP_DEMO_CONFIG=./demo/ci/config-e2e.yaml"
echo "CFOP_GIT_REPO=cfop-demo/scratch"
# up.sh --remediate requires a token, and the executor refuses to start
# without one. The stub never checks it.
echo "GITHUB_TOKEN=e2e-stub-token"
