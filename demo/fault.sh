#!/usr/bin/env bash
# CFOP-31: inject (and clear) demo faults on the kind cluster.
#
#   demo/fault.sh crashloop   # CrashLoopBackOff pod
#   demo/fault.sh oomkill     # pod OOMKilled against its own limit
#   demo/fault.sh taint       # taint all nodes + a pod with no toleration
#                             #   -> Pending (exercises the add-toleration
#                             #      proposer under the remediate variant)
#   demo/fault.sh clear       # remove taints and every injected fault
#
# Each injection stamps a unique pod name: the memory beat is "same fault
# class, different pod", so the second alert is similar to — not identical
# to — the first, which is exactly what the KB's similar-investigation
# search is for.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

export KUBECONFIG="$PWD/demo/.kube/host-config"
NS=demo-faults
TAINT_KEY=cfop-demo   # the proposer reads the taint off the cluster, so any key works — keep it greppable

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
die()  { printf '\n\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[[ -f demo/.kube/host-config ]] || die "no demo kubeconfig — run demo/up.sh first"
kubectl get nodes >/dev/null 2>&1 || die "kind cluster unreachable — run demo/up.sh"

inject() { # $1=manifest basename  $2=name prefix
  # `clear` deletes the namespace without waiting; an immediate re-inject (the
  # memory beat) would try to create content in a Terminating namespace and
  # fail. Wait out the termination instead of making the reader time it.
  for _ in $(seq 1 30); do
    [[ "$(kubectl get ns "$NS" -o jsonpath='{.status.phase}' 2>/dev/null || true)" == "Terminating" ]] || break
    sleep 2
  done
  kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS" >/dev/null
  local name="$2-$(date +%H%M%S)"
  sed "s/__NAME__/$name/" "demo/manifests/$1" | kubectl apply -f - >/dev/null
  echo "$name"
}

case "${1:-}" in
  crashloop)
    name=$(inject crashloop.yaml boom)
    say "Injected CrashLoopBackOff: $NS/$name"
    note "DemoPodCrashLooping fires in ~1–2 minutes; event_runtime picks it up on its next Alertmanager poll."
    ;;
  oomkill)
    name=$(inject oom.yaml hog)
    say "Injected OOM fault: $NS/$name (32Mi limit, allocating until killed)"
    note "DemoPodOOMKilled fires shortly after the first kill."
    ;;
  taint)
    say "Tainting all nodes with $TAINT_KEY=fault:NoSchedule"
    kubectl taint nodes --all "$TAINT_KEY=fault:NoSchedule" --overwrite >/dev/null
    name=$(inject pending.yaml stuck)
    say "Injected unschedulable pod: $NS/$name"
    note "DemoPodUnschedulable fires in ~1–2 minutes. Under the remediate"
    note "variant the proposer answers with an add-toleration patch and the"
    note "executor opens a PR against \$CFOP_GIT_REPO — merge it, then run"
    note "  demo/fault.sh clear   # the reconciler marks the row resolved"
    ;;
  clear)
    say "Clearing demo faults"
    kubectl taint nodes --all "$TAINT_KEY-" >/dev/null 2>&1 || true
    kubectl delete ns "$NS" --ignore-not-found --wait=false >/dev/null
    note "Namespace $NS deleted; taints removed. Alerts resolve on the next evaluation."
    ;;
  *)
    die "usage: demo/fault.sh crashloop|oomkill|taint|clear"
    ;;
esac
