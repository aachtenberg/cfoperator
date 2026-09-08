#!/usr/bin/env bash
# Cockpit pod entrypoint (CFOP-35): become the briefed session.
#
# Everything incident-specific arrives as environment: the investigation id,
# the in-cluster agent URL, and the per-investigation session token (from a
# Secret, never from a manifest env value). The pod then fetches its own
# briefing — so the manifest carries no findings, and the credential that reads
# them dies with this Job.
set -euo pipefail

: "${CFOP_INVESTIGATION_ID:?cockpit needs an investigation id}"
: "${CFOP_AGENT_URL:?cockpit needs the in-cluster agent URL}"
: "${CFOP_API_TOKEN:?cockpit needs its session token (mounted from a Secret)}"

# CFOP-146: plain kubectl does not read a pod's service-account token. Without
# a kubeconfig it talks to localhost:8080 and the read-only SA is unreachable.
sa=/var/run/secrets/kubernetes.io/serviceaccount
if [ -s "$sa/token" ] && [ -n "${KUBERNETES_SERVICE_HOST:-}" ]; then
  mkdir -p "${HOME}/.kube"
  umask 077
  {
    printf '%s\n' 'apiVersion: v1' 'kind: Config'
    printf '%s\n' 'clusters:' '- name: local' '  cluster:'
    printf '    server: https://%s:%s\n' \
      "${KUBERNETES_SERVICE_HOST}" "${KUBERNETES_SERVICE_PORT:-443}"
    printf '    certificate-authority: %s\n' "$sa/ca.crt"
    printf '%s\n' 'users:' '- name: cockpit' '  user:'
    printf '    token: "%s"\n' "$(tr -d '\n' < "$sa/token")"
    printf '%s\n' 'contexts:' '- name: cockpit' '  context:' \
      '    cluster: local' '    user: cockpit' 'current-context: cockpit'
  } > "${HOME}/.kube/config"
  echo "kubectl: in-cluster, read-only service account"
fi

# Stage the session SSH identity. Pod: files on the token Secret at /ssh-secret.
# Container: the same files as CFOP_COCKPIT_SSH_BUNDLE (base64 JSON in the
# env-file). Either way they land in ~/.ssh at 0600 — ssh refuses a
# group-readable private key, and a secret volume is at best 0440.
stage_ssh_tree() {
  src=$1
  [ -d "$src" ] || return 1
  mkdir -p "${HOME}/.ssh"
  chmod 700 "${HOME}/.ssh"
  staged=0
  for f in "$src"/*; do
    [ -f "$f" ] || continue
    name=$(basename "$f")
    case "$name" in
      CFOP_API_TOKEN|known_hosts|authorized_keys) continue ;;
    esac
    cp "$f" "${HOME}/.ssh/$name"
    chmod 600 "${HOME}/.ssh/$name"
    staged=1
  done
  if [ -f "${HOME}/.ssh/ssh-privatekey" ] && [ ! -f "${HOME}/.ssh/id_rsa" ]; then
    cp "${HOME}/.ssh/ssh-privatekey" "${HOME}/.ssh/id_rsa"
    chmod 600 "${HOME}/.ssh/id_rsa"
  fi
  [ "$staged" = 1 ]
}

if stage_ssh_tree /ssh-secret; then
  :
elif [ -n "${CFOP_COCKPIT_SSH_BUNDLE:-}" ]; then
  python3 -c '
import base64, json, os, pathlib
raw = os.environ.get("CFOP_COCKPIT_SSH_BUNDLE") or ""
try:
    files = json.loads(base64.b64decode(raw))
except Exception:
    raise SystemExit(0)
if not isinstance(files, dict):
    raise SystemExit(0)
dest = pathlib.Path.home() / ".ssh"
dest.mkdir(mode=0o700, exist_ok=True)
skip = {"CFOP_API_TOKEN", "known_hosts", "authorized_keys"}
for name, body in files.items():
    if name in skip or not isinstance(body, str) or "/" in name or name.startswith("."):
        continue
    path = dest / name
    path.write_text(body)
    path.chmod(0o600)
priv = dest / "ssh-privatekey"
rsa = dest / "id_rsa"
if priv.is_file() and not rsa.is_file():
    rsa.write_text(priv.read_text())
    rsa.chmod(0o600)
'
fi
if [ -f "${HOME}/.ssh/config" ]; then
  echo "ssh: inventory hosts from ~/.ssh/config (same names as infrastructure.hosts)"
fi

# --no-session-token: this process *is* the session, and it already holds a
# credential that expires with the pod's activeDeadlineSeconds. Letting cfassist
# mint a second one would create a token whose revoke-on-exit never runs when
# the Job is killed by its deadline — precisely the orphaned credential the
# short TTL exists to prevent.
args=(attach "${CFOP_INVESTIGATION_ID}"
      --agent-url "${CFOP_AGENT_URL}"
      --no-session-token)

# The model the cockpit talks to. Unset leaves cfassist's own default, which is
# the right failure: a clear "cannot reach the LLM" beats a silent connection to
# something the operator did not choose.
if [ -n "${CFOP_COCKPIT_LLM_URL:-}" ]; then
  args+=(--url "${CFOP_COCKPIT_LLM_URL}")
fi
if [ -n "${CFOP_COCKPIT_LLM_MODEL:-}" ]; then
  args+=(--model "${CFOP_COCKPIT_LLM_MODEL}")
fi

echo "cockpit — investigation #${CFOP_INVESTIGATION_ID} — ${CFOP_COCKPIT_PLACEMENT:-placement unrecorded}"
echo "read-only service account; this pod and its token die with the session."

exec cfassist "${args[@]}"
