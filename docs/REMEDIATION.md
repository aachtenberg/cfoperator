# Remediation queue + portable executor

Turns an investigation's recommendation into a **proposed PR** that a human
merges (ArgoCD then syncs). Read-only toward the cluster end to end — the only
mutation is the PR.

## Pipeline

```
deep investigation (worker)            agent (CFOperator pod)                 executor Job
  emits in details:                      store_deep_investigation()             (cfoperator-executor image)
    remediation_class                      └─ _maybe_queue_remediation ──┐        render remediation.md
    risk / confidence            POST        (queue_feed)                │        → swappable LLM → diff
    recommendation        ──────────────▶  RemediationQueue (postgres)  │        → open PR (own stdlib client)
    proposed_diff          /v1/deep-          │                         │        → POST /v1/remediations/{id}/complete
                            investigations    │  drainer (queue_drain) ──┴──spawn──▶  │
                                              │  reaper  (queue_reap, recovers leases)  │
                                              │  reconcile (queue_verify) ◀── PR merged/closed
                                              ▼
                                      update_remediation_status → resolved | needs-human | rejected
```

State machine: `queued → claimed → executing → pr-open → (merge) → verifying → resolved`;
`* → failed → (retry) queued | needs-human`; `pr-open → rejected` (PR closed unmerged).
Non-auto-eligible recommendations are recorded directly as `needs-human`.

**Auto-execute gate** (`remediation.queue_feed` enqueues; gate in `knowledge_base.remediation_is_auto_eligible`):
only `remediation_class ∈ {gitops-patch, k8s-action}` **and** `risk == low` **and**
`confidence ≥ 0.8` enter `queued`. Everything else → `needs-human`.

## Flags (config.yaml `remediation:`)

| flag | default | effect |
|---|---|---|
| `queue_reap` | true | recover dead executor leases (safe/idempotent) |
| `queue_feed` | false | enqueue remediations from investigation hints |
| `queue_drain` | false | claim queued items + spawn executor Jobs |
| `queue_verify` | false | advance `pr-open` rows by PR merge/close state |

Go-live order: `queue_feed` → watch rows land → `queue_drain` → `queue_verify`.

## LLM backend (swappable, per executor)

Set under `remediation.executor.llm` (or env on the Job): `backend` =
`anthropic` | `openai` | `claude-cli`. `openai` speaks /chat/completions so it
covers Ollama, the homelab llm-gateway, vLLM, OpenAI — set `base_url`/`model`.

## Deploy requirements (private `cfoperator-deploy` repo)

1. **Image/CI** — build & push `ghcr.io/aachtenberg/cfoperator-executor:main`
   from `executor/Dockerfile` (multi-arch, same pattern as cfoperator-worker).

2. **Executor ServiceAccount + RBAC — read-only k8s only.** No write verbs; the
   PR is the only mutation.
   ```yaml
   apiVersion: v1
   kind: ServiceAccount
   metadata: { name: cfoperator-executor, namespace: apps }
   ---
   apiVersion: rbac.authorization.k8s.io/v1
   kind: ClusterRole
   metadata: { name: cfoperator-executor-readonly }
   rules:
     - apiGroups: ["", "apps"]
       resources: ["pods","events","configmaps","deployments","nodes"]
       verbs: ["get","list","describe"]
   ---
   apiVersion: rbac.authorization.k8s.io/v1
   kind: ClusterRoleBinding
   metadata: { name: cfoperator-executor-readonly }
   roleRef: { apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: cfoperator-executor-readonly }
   subjects: [{ kind: ServiceAccount, name: cfoperator-executor, namespace: apps }]
   ```

3. **Agent SA must create Jobs.** The drainer runs in the CFOperator pod, so the
   `cfoperator` (agent) ServiceAccount needs `create`/`get`/`list` on
   `batch/jobs` in `apps`. Verify this exists; the deep tier creates Jobs from
   the *event_runtime* SA, which is a different binding.

4. **Secret `cfoperator-secrets` keys** consumed by the executor Job env:
   `GITHUB_TOKEN` (required — opens the PR), `ANTHROPIC_API_KEY` (anthropic/cli
   backends), `CFOP_COMPLETION_SHARED_SECRET` (optional — authenticates the
   callback, same secret the deep callback uses).

5. **Callback reachability** — `remediation.executor.completion_base_url` must
   resolve to the agent's web server (default
   `http://cfoperator.apps.svc.cluster.local:8083/v1/remediations`).

## Run & debug

- Local unit tests: `pytest` in `executor/`, `agent/`, `worker/`.
- Inspect the queue: `GET /api/investigations` for sources; the
  `remediation_queue` table for rows (id, status, remediation_class, pr_url).
- First live smoke: set `queue_feed: true`, trigger a deep investigation that
  emits `REMEDIATION_CLASS: gitops-patch / RISK: low / CONFIDENCE: 0.9`, confirm
  a `queued` row appears. Then `queue_drain: true` and watch for a
  `cfop-executor-*` Job and a `pr-open` transition.
