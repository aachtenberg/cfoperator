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
