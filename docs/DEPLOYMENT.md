# CFOperator Deployment Guide

Production is k3s + ArgoCD GitOps. **`git push` is the deploy path** — no rsync, no SSH, no manual `kubectl apply`.

## Production Layout

| Workload | Manifest | Image | Port |
|----------|----------|-------|------|
| `cfoperator` (agent + console) | cfoperator-deploy | `cfoperator:main-<sha7>` | `8083` (hostNetwork) |
| `cfoperator-event-runtime` | cfoperator-deploy | same image, different `command` | `8080` |
| `cfoperator-mcp` | cfoperator-deploy | same image, `-m mcp_server` | `8090` |
| `cfoperator-bridge` (Slack) | cfoperator-deploy | same image, `-m bridge`, `Recreate` | — (outbound) |
| `cfoperator-changerecord` | cfoperator-deploy | `cfoperator-changerecord` | ClusterIP |
| `cfoperator-executor` | cfoperator-deploy | `cfoperator-executor` — one Job per remediation | — |
| `cfoperator-worker` | cfoperator-deploy | `cfoperator-worker` — deep investigation | — |
| `cfoperator-cockpit` | none (agent builds the Job at spawn) | `cfoperator-cockpit` — one Job per `attach --spawn` | — |

All manifests live in the private **cfoperator-deploy** repo, which ArgoCD's standalone `cfoperator` Application syncs; the public repo holds no topology. All images are `ghcr.io/aachtenberg/…`. Namespace `apps`; both agent pods on `headless-gpu` = `ubuntu-llm-01` = 192.168.0.150 (hostNetwork, so that is also the console's address). Control plane runs `kubectl` locally.

`build-cfoperator-main.yml` builds all five images per run, each pushed as floating `:main` and immutable `:main-<sha7>`. **Only the agent tag auto-bumps**; worker/executor/changerecord/cockpit track `:main`, so wait for the build job — there is nothing to merge for them either. The cockpit build `needs:` worker — it derives from it.

Per-workload config: [mcp-server.md](mcp-server.md), [slack-bridge.md](slack-bridge.md), [REMEDIATION.md](REMEDIATION.md).

## Cockpit: deploy-repo changes

`cfassist attach --spawn` is inert until these land in **cfoperator-deploy**. Nothing else depends on them. (The Helm chart carries the equivalent behind `cockpit.enabled` / `cockpit.ssh.secretName`, for external installs — see [cockpit.md](cockpit.md).)

### 1. Tier 1: pod spawn — `cfoperator-rbac.yml`

Add `secrets: create` to the existing `cfoperator-jobs` Role:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: cfoperator-jobs
  namespace: apps
rules:
- apiGroups: ["batch"]
  resources: ["jobs"]
  verbs: ["create", "get", "list", "watch", "delete"]
# Cockpit (CFOP-35): the pod's short-lived session token is written to a Secret
# the Job references. `create` only, and no `delete` — the Job owns the Secret,
# so GC removes it. (This SA already has cluster-wide `get` on secrets via
# cfoperator-role, so withholding it here narrows nothing.)
- apiGroups: [""]
  resources: ["secrets"]
  verbs: ["create"]
```

Append the cockpit identity (same posture as `cfoperator-worker-readonly`):

```yaml
---
# The cockpit Job's identity (CFOP-35): strictly read-only. Deliberately NO
# exec, NO write verbs, NO secrets, NO configmaps — a cockpit is a place to
# look from; the write path stays the PR/console gate.
apiVersion: v1
kind: ServiceAccount
metadata:
  name: cfoperator-cockpit
  namespace: apps
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: cfoperator-cockpit-readonly
rules:
- apiGroups: [""]
  resources: ["pods", "pods/log", "services", "endpoints", "namespaces", "nodes", "events", "persistentvolumeclaims"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["apps"]
  resources: ["deployments", "replicasets", "statefulsets", "daemonsets"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["batch"]
  resources: ["jobs", "cronjobs"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["metrics.k8s.io"]
  resources: ["nodes", "pods"]
  verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: cfoperator-cockpit-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cfoperator-cockpit-readonly
subjects:
- kind: ServiceAccount
  name: cfoperator-cockpit
  namespace: apps
```

Missing → `kubectl create failed: jobs is forbidden` / `secrets is forbidden` at spawn.

### 2. Tiers 2/3: non-cluster hosts — `cfoperator.yml`

**One env var.** Add to the `cfoperator` container's `env:`:

```yaml
            # Cockpit tiers 2/3 (CFOP-36): the agent URL a session on a
            # NON-cluster host calls. Not cockpit.agent_url — that is cluster
            # DNS, which a Pi cannot resolve. Host tiers 400 until this is set.
            - name: CFOP_COCKPIT_HOST_AGENT_URL
              value: "http://192.168.0.150:8083"
```

### 3. The in-pod model URL — `cfoperator.yml`

The session inherits the agent's `llm.primary.url` so it talks to the model the
investigation ran on. That is `${OLLAMA_DIRECT_URL}` = `http://localhost:11434`,
which is correct **only** from the agent pod — it is `hostNetwork: true` on the
box ollama runs on. A cockpit pod is not, so its `localhost` is itself. Set the
address the *cluster* reaches ollama at:

```yaml
            # The in-pod cockpit session's model. NOT OLLAMA_DIRECT_URL — that
            # is loopback, correct for this hostNetwork agent and meaningless in
            # a normal pod, whose localhost is itself.
            - name: CFOP_COCKPIT_LLM_URL
              value: "http://192.168.0.150:11434"
```

Confirm the value from any host that is not the LLM box (the control plane will
do) before setting it — if this refuses, ollama is bound loopback-only and no
pod can reach it either:

```bash
curl -fsS http://192.168.0.150:11434/api/tags | head -c 200
```

Missing → the spawn is refused with this key named. Before that guard existed it
attached fine and then failed inside the session with `dial tcp [::1]:11434:
connect: connection refused`, which points at ollama rather than at the config.

Tiers `host` and `ssh` are exempt: those sessions run directly on the machine,
so loopback is right there whenever ollama is on that host.

**The ssh key needs no work.** The `ssh-perms` initContainer already stages `cfop-forensics-ssh` into `/root/.ssh/id_rsa` (0600, root-owned), both containers mount that emptyDir, and every `infrastructure.hosts[*].ssh.key_path` in the ConfigMap already points at it. `CFOP_COCKPIT_SSH_SECRET_DIR` / `CFOP_COCKPIT_SSH_USER` exist for installs without that staging — this one does not need them.

Missing → tier 1 still works; host tiers report "the affected host could not be probed" and fall back to a cluster pod.

### Verify

```bash
kubectl -n apps get sa cfoperator-cockpit
kubectl auth can-i create jobs    -n apps --as=system:serviceaccount:apps:cfoperator   # yes
kubectl auth can-i create secrets -n apps --as=system:serviceaccount:apps:cfoperator   # yes
kubectl auth can-i create pods/exec -n apps \
  --as=system:serviceaccount:apps:cfoperator-cockpit                                   # no

# End to end against a real investigation id
cfassist attach <id> --spawn                                   # tier 1: pod
cfassist attach <id> --spawn --host raspberrypi4 --tier ssh    # host tier smoke test
kubectl -n apps get jobs -l cfop.dev/role=cockpit
```

`can-i get secrets` also answers **yes**. That is pre-existing and not something
the cockpit added — `cfoperator-role` lists `secrets` in its cluster-wide read
rule. So the "`create` only, so this is not a way to read secrets" reasoning is
the *chart's*, where the agent has no secrets read; it does not describe this
deployment. `create` is still the minimal addition, it is just not the thing
standing between the agent and a secret. See [Agent secrets read](#agent-secrets-read).

## Agent secrets read

`cfoperator-role` grants the agent SA cluster-wide `get`/`list`/`watch` on
`secrets` and `configmaps` — predating the cockpit and unrelated to it:

```yaml
- apiGroups: [""]
  resources: ["pods", "pods/log", "services", "endpoints", "namespaces", "nodes", "events", "configmaps", "secrets", "persistentvolumeclaims"]
  verbs: ["get", "list", "watch"]
```

**No tool currently reaches secret values.** The only tool taking an arbitrary
resource type is `k8s_describe`, which runs `kubectl describe` — that prints key
names and byte counts, not contents. Every `-o json` call in `tools/k8s.py` is
hardcoded to pods / deployments / services / ingresses / events / nodes /
namespaces.

So it is a **latent grant held by convention, not by RBAC**: one generically
typed `-o json` tool away from being a live exposure. The Helm chart does not
grant it. Narrowing means dropping `secrets` (and probably `configmaps`) from
that rule and confirming nothing regresses — worth doing deliberately, not as a
side effect of an unrelated change.

## What the image contains

The Dockerfile COPYs named paths, not the tree: `cfshared/`, `agent/`, `tools/`, `skills/`, `ui/`, `observability/`, `event_runtime/`, `mcp_server/`, `bridge/`, `auth/`, `scripts/`, `web_server.py`, `web_auth.py`, `cockpit_spawn.py`, `cockpit_ladder.py`.

Most are imported at module load, so a missing COPY crash-loops a pod rather than degrading it — `cfshared/` (agent + event-runtime), `auth/` (agent + MCP), `web_auth.py`, `cockpit_*.py`. `scripts/create_admin.py` is the lockout recovery path ([auth.md](auth.md#locked-out--no-usable-admin)).

`test_dockerfile_image.py` enforces this. **Add a `COPY` for any new top-level package.**

## How a Code Change Reaches Production

[`tests.yml`](../.github/workflows/tests.yml) runs on every PR and push to `main` — the only automated gate; the build workflow runs no tests.

**There is nothing to merge.** Push to `main` and it deploys:

1. [`build-cfoperator-main.yml`](../.github/workflows/build-cfoperator-main.yml) builds and pushes `linux/amd64` images.
2. The `bump-deploy-repo` job commits the new `:main-<sha7>` straight to **cfoperator-deploy**'s `main` (`kustomize edit set image`).
3. ArgoCD syncs within ~3 min (`selfHeal: true` reverts manual `kubectl edit`) and both agent pods roll.

Total: ~5 min from push. A `v*` tag build publishes versioned images but **does not** bump the deploy repo — pointing ArgoCD at a release tag would freeze the fleet on it.

Force a sync instead of waiting:

```bash
kubectl -n argocd annotate application cfoperator \
  argocd.argoproj.io/refresh=hard --overwrite
kubectl -n argocd get application cfoperator
```

`cfoperator` is its own Application (`k3s/base/argocd/applications/cfoperator.yml` in homelab-infra), pointed at cfoperator-deploy — not part of `homelab-root`.

### Workflow `paths-ignore`

Skipped: `**.md`, `docs/**`, `benchmarks/**`, `llm-gateway/**`, `grafana/**`. A push touching only these produces no image — intentional, but worth knowing when you expect a tag and none appears.

**A path may only be ignored if the Dockerfile does not COPY it.** Otherwise the change produces no image and no rollout: the fix appears to deploy, doesn't, and lands later attributed to an unrelated commit. `cfshared/**` and `observability/**` broke this and were removed; `cfassist-go/**` left the list when the cockpit image began baking it in, so a CLI-only change now rebuilds all five. `test_dockerfile_image.py` cross-checks the list against the COPY set.

## Common Change Types

| Change | What ships | What you do |
|--------|------------|-------------|
| `agent/`, `tools/`, `skills/`, `ui/`, `event_runtime/`, `observability/`, `web_server.py` | New agent tag | Push to main. Deploys itself. |
| `auth/`, `web_auth.py`, `scripts/` | New agent tag | Same. |
| `mcp_server/`, `bridge/` | New agent tag | Same — both reuse the agent image. |
| `worker/`, `executor/`, `changerecord/`, `cockpit/`, `cfassist-go/` | New sibling tags | Same push, separate build jobs. These track `:main` — **wait for the build**, don't merge a bump PR (a Job re-queued too early pulls the prior `:main`). |
| `requirements.txt`, `Dockerfile` | New agent tag | Same. A new top-level package needs a `COPY`. |
| cfoperator-deploy YAML (manifests, RBAC) | ArgoCD apply | Push to cfoperator-deploy/main. |
| homelab-infra YAML (everything else in the cluster) | ArgoCD apply | Push to homelab-infra/main. |
| Grafana dashboard | Provisioned ConfigMap | Edit `k3s/base/monitoring/files/grafana-dashboards/`, push. |
| Agent config (`remediation`, `ooda.noise`, `llm`) | `cfoperator-config` ConfigMap | Push to cfoperator-deploy/main, **then restart the pod** — config is read at start, so ArgoCD syncing the ConfigMap alone changes nothing. Runtime flags that live in the DB toggle live from the console instead and need no restart. |
| Secret value | Re-sealed SealedSecret | Edit `homelab-infra/secrets/.env.secrets`, run `homelab-infra/scripts/seal-secrets.sh`, push. |

## Verification

```bash
kubectl get pods -n apps -l 'app.kubernetes.io/name in (cfoperator,cfoperator-event-runtime)'

# What image is actually running
kubectl get deploy -n apps cfoperator cfoperator-event-runtime \
  -o jsonpath='{range .items[*]}{.metadata.name}{": "}{.spec.template.spec.containers[*].image}{"\n"}{end}'

kubectl logs -n apps deploy/cfoperator -f

# event-runtime (via pod IP; the agent is hostNetwork)
pod_ip=$(kubectl get pod -n apps -l app.kubernetes.io/name=cfoperator-event-runtime \
  -o jsonpath='{.items[0].status.podIP}')
curl -fsS "http://${pod_ip}:8080/health"
curl -fsS "http://${pod_ip}:8080/metrics" | grep cfoperator_event_runtime
```

## Console Auth

`:8083` authenticates against `auth_users` / `auth_api_tokens` in the KB database — `admin` / `member` roles, revocable tokens with `read` ⊂ `investigate` ⊂ `remediate`. Managed at `/admin` and `/account`.

Tables are created on start; the first admin seeds from the existing `CFOP_UI_USERNAME` / `CFOP_UI_PASSWORD_HASH` sealed secret, so shipping it locks nobody out.

**The shared `CFOP_API_TOKEN` was retired 2026-08-09. Do not re-add it.** event-runtime, MCP and bridge each hold their own database token, mounted *as* that env var, so callers are unchanged and revoking one breaks only that service.

No auth database → legacy env credentials. Configured but unreachable → 503 on every non-exempt route; never 401, never open.

Roles, rollout order and lockout runbooks: [auth.md](auth.md).

### The cockpit bridge port (`:8084`)

Off by default and closed until you open it — `cockpit.bridge_enabled`, plus
`cockpit.bridge_origins`, without which it refuses to listen at all. See
[config-reference.md](config-reference.md); what it does is in
[cockpit.md](cockpit.md).

Before enabling it anywhere real: **`:8084` needs the same host-level guard
`:8083` has.** The agent pod is `hostNetwork`, so a NetworkPolicy cannot reach
either port — the restriction lives in the host firewall, not in Kubernetes,
and a new port is not covered by the existing rule. Extend that rule to 8084
for the same sources before flipping the flag; nothing in the chart or the
manifests will do it for you, and nothing will warn you.

Two things bound the blast radius if you get that wrong, and neither is a
substitute for the rule: a connection needs an `investigate`-scoped token, and
a browser Origin on the allowlist. The bridge cannot spawn a cockpit — it only
attaches to one that exists — and it serves the host tiers only.

The console is the intended client (CFOP-59): an admin's **Open cockpit**
mints a 120-second one-shot ticket and opens `ws://<console host>:8084` from
the page, so `bridge_origins` must name the console **exactly as operators
reach it** — `http://10.0.0.14:8083`, not the service DNS name — and the
browser must be able to reach `:8084` on that same host. A console reached
through a tunnel or a proxy that does not also forward `:8084` will get the
button and then a refused socket; the drawer says which wall it hit.

**Pod-tier terminals (Phase B) are a second, separate grant.** The host tiers
need no cluster permission — the agent already holds the ssh key. Opening a
browser terminal into a *cockpit pod* requires `create` on `pods/attach`, which
the chart grants only when `cockpit.bridgePodAttach=true` (off by default, its
own switch, never a side effect of `cockpit.enabled`). It pairs with the
runtime `cockpit.bridge_pod_tier`. `test_cockpit_attach_contract.py` holds the
grant's shape — attach-only, create-only, a namespaced Role — so it cannot
quietly widen to `pods/exec` or cluster scope. Leave both off unless you have
decided the console may open pod terminals.

## Local / Non-Production

```bash
docker compose up -d                                    # local agent
python3 -m event_runtime --host 0.0.0.0 --port 8080     # event runtime only
```

See [event-runtime-quickstart.md](event-runtime-quickstart.md).

## Prerequisites

| File | Purpose |
|------|---------|
| `config.yaml` | local dev config |
| `.env` | local dev secrets |
| `homelab-infra/secrets/.env.secrets` | source of truth for cluster secrets |
| `~/.ssh/id_rsa` | SSH access for fleet operations |

| Infra service | Port | Required? |
|---------------|------|-----------|
| PostgreSQL | 5432 | Yes |
| Prometheus | 9090 | Yes |
| Loki | 3100 | Yes |
| Ollama | 11434 | Yes (or a cloud LLM) |
| Alertmanager | 9093 | Optional |
