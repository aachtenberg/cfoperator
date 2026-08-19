# Reproducible kind demo

Break a real Kubernetes cluster on your laptop, watch CFOperator investigate
it, break it the same way again and watch the second investigation cite the
first. Optionally, let it propose the fix as a pull request you merge.

Everything runs locally: the cluster is [kind](https://kind.sigs.k8s.io/)
(Kubernetes in Docker), the monitoring is kube-prometheus-stack, and
CFOperator is the same trial compose the quickstart uses — no cloud account,
no data leaving the machine. This directory is also the release e2e harness
(`.github/workflows/demo-e2e.yml` runs the same scripts against a scripted
LLM).

> **Note:** until the Helm chart ships, CFOperator itself runs in docker
> compose joined to kind's network rather than inside the cluster. The demo
> flow is identical; only the install command will change.

## Prerequisites

- Docker, [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation),
  kubectl, helm
- An LLM: a local [Ollama](https://ollama.com) with a tool-calling model
  pulled (and `nomic-embed-text` for the memory beat), or an API key —
  same `.env` knobs as the trial compose (`OLLAMA_URL`, `OLLAMA_MODEL`,
  `ANTHROPIC_API_KEY`, …)
- ~6 GB of RAM to spare for the cluster + monitoring stack

## The 15 minutes

```sh
demo/up.sh                  # kind + kube-prometheus-stack + CFOperator (~5 min)
demo/fault.sh crashloop     # break something real
```

Open the console (http://localhost:8083, credentials printed by
`docker compose logs bootstrap`). Within ~2–3 minutes the fault becomes a
firing alert, event_runtime polls it out of Alertmanager, triage classifies
it, and an investigation lands on the *Investigations* page with tool calls
against your kind cluster.

**The memory beat.** Once the first investigation completes:

```sh
demo/fault.sh clear
demo/fault.sh crashloop     # same fault class, different pod
```

The second investigation's drawer shows **Similar past investigations** —
the first one, cited with its similarity score. That is the institutional
memory a fresh install cannot show you: this instance has seen this class of
failure before, and every future investigation starts from that. The
citations are recorded facts (`findings.similar_past`), not model prose.

Then take the incident over in your terminal, pre-briefed:

```sh
cfassist attach <investigation-id>
```

Other fault classes: `demo/fault.sh oomkill`, `demo/fault.sh taint`.

## The remediation variant (optional): fault → PR → merge → resolved

This variant lets the executor propose the fix for the unschedulable-pod
fault as a pull request against a **scratch GitHub repo** — the human gate is
the merge button, exactly as in production.

1. Create a scratch repo and copy `demo/manifests/pending.yaml` into it
   (any path; the executor locates it).
2. A token that can push branches + open PRs there:

```sh
export GITHUB_TOKEN=ghp_…
export CFOP_GIT_REPO=you/cfoperator-demo-scratch
export ANTHROPIC_API_KEY=sk-…   # or configure an executor LLM of your choice
demo/up.sh --remediate
demo/fault.sh taint
```

The pod sits Pending on an untolerated taint; the investigation lands
`needs_action`; the proposer recognizes the taint case and the queue drains
it to an executor Job **inside the kind cluster** (`kubectl -n cfop-demo get
jobs` to watch), which opens a PR adding the toleration. Merge the PR, run
`demo/fault.sh clear`, and the reconciler marks the remediation resolved.

## Teardown

```sh
demo/down.sh
```

## Determinism notes

- Chart version is pinned in `up.sh` (`CFOP_DEMO_KPS_VERSION` to override).
- Alert rules are the demo's own (`demo/manifests/demo-rules.yaml`), scoped
  to the `demo-faults` namespace and tuned to fire in ~1 minute; stock
  kube-prometheus-stack rules wait 15 minutes by design.
- Repeat faults get unique pod names so recurrence suppression never eats
  the memory beat.
- CI substitutes `demo/ci/llm_stub.py` for the model and asserts the
  pipeline mechanics only; `test_demo_kind.py` guards the cross-artifact
  agreements (manifest shapes, network aliases, rule scoping, stub/parser
  vocabulary) that would otherwise drift silently.
