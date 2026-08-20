# Helm chart

The k8s-native trial path (CFOP-30): `charts/cfoperator`. The homelab's
private cfoperator-deploy/ArgoCD/SealedSecrets pipeline is unaffected — that
stays the prod path; this chart is the public one. Normal Kubernetes Secrets,
no hostNetwork, no cluster-admin heroics.

## Install

```bash
# from a release (published on version tags):
helm install cfoperator oci://ghcr.io/aachtenberg/charts/cfoperator \
  -n cfoperator --create-namespace -f values.yaml

# or from a checkout:
helm install cfoperator charts/cfoperator -n cfoperator --create-namespace -f values.yaml
```

The getting-started `values.yaml` — same three-values contract as the
compose trial (LLM, Prometheus, optionally notify):

```yaml
llm:
  url: http://ollama.example:11434
  model: llama3.1:8b
prometheus:
  url: http://kps-kube-prometheus-stack-prometheus.monitoring:9090
alertmanager:
  url: http://kps-kube-prometheus-stack-alertmanager.monitoring:9093
notify:
  slackWebhook: https://hooks.slack.com/services/...
```

An LLM and `prometheus.url` are hard-required (install fails without them).
`notify` is optional — blank leaves that sink off. Postgres (pgvector) is
bundled by default; `postgres.bundled: false` + `postgres.external.*` brings
your own (it must allow `CREATE EXTENSION vector`, or embeddings stay off).

Admin credentials: username `admin`; the password is generated and printed
once by the bootstrap Job — `kubectl logs job/<release>-bootstrap` (the
install NOTES show the exact command).

## Profiles

- `profile: investigate` (default) — observe/triage/investigate/notify.
  RBAC is a read-only ClusterRole (get/list/watch); `test_helm_chart.py`
  fails if a write verb ever creeps into it.
- `profile: remediate` — adds the remediation queue and executor Jobs
  (requires `remediate.gitRepo` + `remediate.githubToken`). The agent gains
  write access to exactly one resource: batch Jobs in the release namespace
  (create for the drainer, delete for the reaper). Executor Jobs themselves
  have no RBAC — their only output is a GitHub PR and a completion callback;
  the human gate is the merge button.

## What differs from the compose trial

- Session secret, service API token, and the completion shared secret are
  chart-generated Secrets (stable across upgrades via `lookup`), not
  bootstrap-written files — pods share no volume. The event-runtime → agent
  credential is therefore the legacy `CFOP_API_TOKEN` service-token path
  instead of a DB-minted token — and that is a **known, deliberate trade-off
  you should understand**: the legacy identity carries every scope
  (`read`+`investigate`+`remediate`, which the console treats as
  admin-equivalent over HTTP), where compose mints a `read`+`investigate`
  token. Anyone who can read Secrets or pod env in the release namespace
  holds an HTTP-admin bearer, even on `profile: investigate` — the
  investigate profile's tameness claim is about **cluster RBAC and the
  profile flag ceiling, not this bearer**. Every use is audited as
  `token.legacy_used`, so migration can be finished on evidence; rotating it
  means changing the Secret and restarting both pods. Minting a DB-backed
  scoped token into the Secret from the bootstrap Job is the follow-up that
  retires this.
- The bootstrap Job runs in `CFOP_BOOTSTRAP_DB_ONLY` mode: schema, pgvector,
  admin seeding only.

## Uninstall / reinstall

`helm uninstall` leaves the bundled Postgres PVC behind (StatefulSet
volumeClaimTemplates always do), but deletes the generated-password Secret —
so a reinstall under the same release name mints a new password against the
old database and the pods loop on `password authentication failed`. Either
delete the PVC for a truly fresh start:

```bash
kubectl -n <ns> delete pvc data-<release>-postgres-0
```

or set `postgres.password` explicitly so it survives reinstalls.

## CI

`chart-ci.yml` lints, refuses a chart that templates with no required
values, and does a real `helm install` on kind per chart PR.
`chart-release.yml` publishes to `oci://ghcr.io/aachtenberg/charts` on
version tags (chart version = app version = the tag).
`test_helm_chart.py` (hermetic, in the pytest suite) guards compose↔chart
drift — every compose env name, every `${VAR}` config fill, and the
CFOP-31 `CFOP_EVENT_RUNTIME_ALERTMANAGER_URL` line specifically.
