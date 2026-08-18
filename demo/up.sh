#!/usr/bin/env bash
# CFOP-31: stand up the reproducible kind demo.
#
#   demo/up.sh               # investigate profile (read-only, default)
#   demo/up.sh --remediate   # + executor Jobs and the PR loop (needs
#                            #   GITHUB_TOKEN + CFOP_GIT_REPO, see README)
#
# Builds: a kind cluster + kube-prometheus-stack (pinned) + fast demo alert
# rules, then the CFOperator trial compose joined to kind's docker network.
# Faults come later, on demand: demo/fault.sh crashloop|taint|oomkill.
#
# When the Helm chart ships (CFOP-30), the compose half of this script is what
# gets replaced by `helm install`; the cluster half and every assertion the CI
# harness makes stay as they are.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

CLUSTER=cfop-demo
KPS_VERSION="${CFOP_DEMO_KPS_VERSION:-88.5.0}"   # pinned: the demo is reproducible, not latest
REMEDIATE=0
[[ "${1:-}" == "--remediate" ]] && REMEDIATE=1

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
die()  { printf '\n\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

for bin in docker kind kubectl helm; do
  command -v "$bin" >/dev/null || die "$bin is required (see demo/README.md)"
done

if [[ "$REMEDIATE" == 1 ]]; then
  [[ -n "${GITHUB_TOKEN:-}" ]] || die "--remediate needs GITHUB_TOKEN in the environment"
  [[ -n "${CFOP_GIT_REPO:-}" ]] || die "--remediate needs CFOP_GIT_REPO=owner/scratch-repo"
fi

# ── 1. kind cluster ──────────────────────────────────────────────────────────
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  say "kind cluster '$CLUSTER' already exists — reusing"
else
  say "Creating kind cluster '$CLUSTER'"
  kind create cluster --name "$CLUSTER" --wait 120s
fi

mkdir -p demo/.kube
# Host view (127.0.0.1:mapped-port) for fault.sh and your own kubectl.
kind get kubeconfig --name "$CLUSTER" > demo/.kube/host-config
# In-network view (server name resolves on kind's docker network) for the
# compose'd agent. World-readable on purpose: it is a throwaway demo cluster,
# and the container user is not the file's owner.
kind get kubeconfig --internal --name "$CLUSTER" > demo/.kube/kubeconfig
chmod 644 demo/.kube/kubeconfig demo/.kube/host-config
export KUBECONFIG="$PWD/demo/.kube/host-config"

# ── 2. kube-prometheus-stack + demo rules ────────────────────────────────────
say "Installing kube-prometheus-stack $KPS_VERSION (release 'kps' — demo-rules.yaml matches on it)"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update prometheus-community >/dev/null
helm upgrade --install kps prometheus-community/kube-prometheus-stack \
  --version "$KPS_VERSION" -n monitoring --create-namespace \
  -f demo/kps-values.yaml --wait --timeout 10m
kubectl apply -f demo/manifests/demo-rules.yaml >/dev/null
note "Prometheus:   http://cfop-demo-control-plane:30090 (from the kind network)"
note "Alertmanager: http://cfop-demo-control-plane:30093"

# ── 3. remediate-variant substrate (optional) ────────────────────────────────
COMPOSE_ENV=()
if [[ "$REMEDIATE" == 1 ]]; then
  say "Preparing executor namespace + secrets (remediate variant)"
  kubectl apply -f demo/manifests/executor-setup.yaml >/dev/null
  kubectl -n cfop-demo delete secret cfoperator-secrets --ignore-not-found >/dev/null
  kubectl -n cfop-demo create secret generic cfoperator-secrets \
    --from-literal=GITHUB_TOKEN="$GITHUB_TOKEN" \
    ${ANTHROPIC_API_KEY:+--from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"} >/dev/null
  export CFOP_DEMO_CONFIG=./demo/config-remediate.yaml
  note "profile: remediate — executor Jobs run in kind ns cfop-demo, PRs target $CFOP_GIT_REPO"
fi

# ── 4. CFOperator trial compose, joined to the kind network ──────────────────
say "Starting CFOperator (trial compose + demo override)"
docker compose -f docker-compose.yml -f demo/docker-compose.demo.yml up -d --build

say "Waiting for the agent to come up (first boot initializes the KB — a few minutes)"
for i in $(seq 1 120); do
  if curl -fsS "http://localhost:${CFOP_CONSOLE_PORT:-8083}/api/health" >/dev/null 2>&1; then
    break
  fi
  [[ "$i" == 120 ]] && die "agent did not become healthy — docker compose logs agent"
  sleep 5
done

say "Demo is up"
note "Console:  http://localhost:${CFOP_CONSOLE_PORT:-8083}  (admin credentials: bootstrap printed them once — docker compose logs bootstrap)"
note "Inject a fault:        demo/fault.sh crashloop"
note "Fire the memory beat:  demo/fault.sh crashloop   # again, after the first investigation completes"
note "Unschedulable + PR:    demo/fault.sh taint       # remediate variant"
note "When an investigation lands, take it over in your terminal:"
note "    cfassist attach <investigation-id>"
