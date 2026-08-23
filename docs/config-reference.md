# Configuration Reference

`config.yaml.example` is the getting-started file: about a dozen settings, no
host inventory, everything else defaulted. This document is the other end of the
scale — every option that exists, in the long form, with the reasoning attached.

For *what infrastructure CFOperator can talk to* and how to write a new backend,
see [infrastructure-config.md](infrastructure-config.md). That file owns the
shipped / planned / not-planned matrix and is the source of truth for backend
names. This file owns config mechanics.

## How the file is read

Your config is **merged over a complete default schema** (`cfshared/config.py`),
not used in place of it. Three rules:

| | |
|---|---|
| **Dicts merge, recursively** | Setting `observability.metrics.url` leaves `observability.metrics.backend` at its default. |
| **Lists and scalars replace** | Writing `observability.notifications` replaces the whole list. Lists here are always an ordered choice you made — the sink chain, the LLM fallback chain, the container backends — so merging them element-wise would make "delete the default" impossible to express. |
| **Defaults never guess an endpoint** | Optional backends default to `url: ""`, and an empty URL is the *disabled* state. So omitting `loki:` disables log queries; it does not point a Loki client at a host that does not exist. |

`${VAR}` is expanded when a value is *entirely* a placeholder, so a password
containing a literal `$` is safe. An unset variable becomes `""`, which every
consumer reads as "not configured". A `.env` file sitting next to `config.yaml`
is loaded first, and the real environment always wins over it.

Both the agent and the event runtime go through this one loader. They used to
have separate ones with different ideas about defaults, which is how they drifted.

### Consequences worth knowing

* A malformed or non-mapping `config.yaml` logs an error and falls back to
  defaults rather than raising at import time. A stack trace three layers into
  startup is harder to diagnose than a running agent saying what it ignored.
* Sections you omit are still present in the resolved config, with default
  values. Code reading `config.get('x', {}).get('y')` gets the default, not
  `None`.
* Because a merged key always exists, a call site's own `.get(key, fallback)`
  no longer reaches its fallback. Defaults in `cfshared/config.py` are therefore
  chosen to match the call-site literal exactly, and keys with no site-neutral
  value (`remediation.default_repo`) are deliberately left out of the schema.

## Profiles

```yaml
profile: investigate    # investigate | remediate
```

A profile is a ceiling on the existing `read ⊂ investigate ⊂ remediate` scope
ladder (the same ladder `auth/models.py` uses for console tokens), not a
separate concept.

| `profile` | Effect |
|---|---|
| `investigate` | Observe, triage, investigate, notify. Every `remediation.*` flag is forced off — including a flag toggled live from the operator console, which would otherwise be a way around the profile. |
| `remediate` | The `remediation.*` flags below take effect as written. |
| *(key absent)* | Unprofiled: flags exactly as written. This is what configs written before profiles existed expect, and it is why the key does not default to `investigate` — doing so would silently disarm an existing remediation deployment on its next routine deploy. |

An unrecognised value falls back to `investigate`, so a typo cannot arm
remediation. A config file that is missing entirely resolves to `investigate`.

The deep-investigation tier is **not** clamped by `investigate`: it is read-only
by design, and anything it proposes goes through the remediation PR gates that
*are* clamped.

## Discovery, and the optional host inventory

No host inventory is required. Workloads come from the Kubernetes API and from
Prometheus targets at runtime. When `observability.containers` is unset and the
process can see it is running inside a cluster (`KUBERNETES_SERVICE_HOST`), the
Kubernetes backend is assumed; otherwise no container backend is configured.
That autodetection cannot override a choice you made — it only fires when the
config says nothing about containers at all.

A static `infrastructure.hosts` block is an **optional overlay** that enables the
hybrid-fleet tier (SSH tools, host discovery tools, baseline drift against
expected services). Its absence is a first-class state: those tools log that they
are disabled and everything else runs. See
[infrastructure-config.md](infrastructure-config.md#infrastructure-hosts) for the
shape and the SSH prerequisites.

## Extras

These are the features that used to make the example config long. All are
optional, none is required for a healthy deployment, and each stays at the config
path it has always used — renaming them under an `extras:` key would have been a
breaking change to existing configs for cosmetic gain.

| Feature | Config path | Docs |
|---|---|---|
| SSH host inventory / hybrid fleet | `infrastructure.hosts`, `event_runtime.host_observability.providers` | [infrastructure-config.md](infrastructure-config.md) |
| Deep-investigation tier | `event_runtime.deep_investigation` | [deep-investigation.md](deep-investigation.md) |
| Remediation queue, executor, PRs | `remediation` | [remediation-pipeline.md](remediation-pipeline.md) |
| Incident cockpit (`attach --spawn`) | `cockpit` | [cockpit.md](cockpit.md) |
| Git / GitHub repo mapping | `git` | [remediation-pipeline.md](remediation-pipeline.md) |
| Event-runtime persistence & scheduling | `event_runtime.persistence`, `event_runtime.scheduler` | [event-runtime-quickstart.md](event-runtime-quickstart.md) |
| MCP server | env-driven | [mcp-server.md](mcp-server.md) |
| Slack Socket Mode bridge | env-driven | [slack-bridge.md](slack-bridge.md) |

## The full config

Everything below is optional. This is the long form of the same file — the
annotated kitchen sink that `config.yaml.example` used to be.

```yaml
# Observability Backends
#
# Every backend named here is one that ships and is registered in
# `observability/__init__.py`. This deliberately lists no aspirational
# alternatives: if a backend is not named below, it does not exist yet, and a
# config naming it fails at startup. See docs/infrastructure-config.md for the
# shipped / planned / not-planned matrix and for how to write your own.
observability:
  # Metrics backend. Shipped: prometheus
  metrics:
    backend: prometheus
    url: http://prometheus:9090
    timeout: 30

  # Logs backend. Shipped: loki
  logs:
    backend: loki
    url: http://loki:3100
    timeout: 30

  # Container runtimes - one or many (like notifications).
  # Shipped: kubernetes, docker, prometheus (bare-metal discovery)
  containers:
    - backend: kubernetes
    - backend: prometheus    # bare-metal Docker via Prometheus discovery + SSH
      ssh_user: sre
    # - backend: docker      # direct Docker API
    #   hosts:
    #     local: unix:///var/run/docker.sock
    #     worker-1: tcp://worker-1:2375

  # Alerts backend. Shipped: alertmanager
  alerts:
    backend: alertmanager
    url: http://alertmanager:9093

  # Notifications backend - choose one or many:
  notifications:
    - backend: slack
      webhook_url: ${SLACK_WEBHOOK_URL}
    - backend: discord
      webhook_url: ${DISCORD_WEBHOOK_URL}
    # ntfy: delivers the triage 'notify' action to a phone/desktop. url + topic
    # are required and env-driven so the (access-controlling) topic stays out of
    # the repo — set NTFY_URL/NTFY_TOPIC (+ optional NTFY_TOKEN) in the deploy.
    # Empty url or topic => sink skipped. All other fields are optional and fall
    # back to the sink's built-in defaults; override here to customize:
    #   priority_map:  {info: "3", warning: "4", critical: "5"}   # ntfy 1..5
    #   tags_map:      {info: information_source, warning: warning, critical: rotating_light}
    #   title: "CFOperator Event Runtime"
    #   timeout: 10
    - backend: ntfy
      url: ${NTFY_URL}
      topic: ${NTFY_TOPIC}
      token: ${NTFY_TOKEN}

# LLM Configuration
llm:
  # Primary LLM (Ollama local)
  primary:
    provider: ollama
    url: http://localhost:11434
    model: qwen3:14b/no_think  # /no_think disables deliberative mode
    timeout: 180  # seconds; generous default for cold model loads

  # Fallback chain (on error/timeout)
  fallback:
    - provider: groq
      model: llama-3.3-70b-versatile
      api_key: ${GROQ_API_KEY}
    - provider: xai          # xAI Grok — OpenAI-compatible API
      model: grok-3
      api_key: ${XAI_API_KEY}
    - provider: gemini
      model: gemini-2.0-flash-exp
      api_key: ${GEMINI_API_KEY}
    - provider: anthropic
      model: claude-3-5-sonnet-20241022
      api_key: ${ANTHROPIC_API_KEY}

  # Optional dedicated triage classifier (an ollama model tag, served from
  # llm.primary.url). When set, alert triage tries this model first and falls
  # back to the normal chain on any failure or unparseable response; when
  # unset, triage uses the primary chain unchanged. Investigations are
  # unaffected. The console Admin -> LLM tab can override this live (DB over
  # config; 'off' there disables despite this key). (CFOP-57/58)
  # triage_model: cfop-triage-ministral3:v1-q4

  # Embeddings (for semantic search)
  embeddings:
    provider: ollama
    url: http://localhost:11434
    model: nomic-embed-text

# Database
database:
  host: postgres
  port: 5432
  database: cfoperator
  user: cfoperator
  password: ${POSTGRES_PASSWORD}

# OODA Loop Configuration
ooda:
  # Reactive mode: check for alerts every N seconds
  alert_check_interval: 10

  # Tier-1 noise reduction (see docs/noise-reduction.md). When an alert is about
  # a recoverable runtime condition (restart/terminated/exit-code/not-ready/
  # crashloop/oom, or a readiness/liveness/startup probe failure) and the pod is
  # healthy right now:
  #   - skip the deep investigation entirely (early-exit to 'monitoring'), and
  #   - downgrade any needs_action that recovered during investigation.
  # Flapping and still-broken pods still investigate. Each class has its own
  # flapping guard: restarts leave a trace in restartCount, while a probe
  # restarts nothing, so the probe class asks how long the pod has held Ready.
  noise:
    enabled: true
    recovered_restart_threshold: 3       # restart class: > this many restarts = flapping
    recovered_ready_stable_seconds: 600  # probe class: Ready must have held this long

  # Proactive mode: deep sweep every N seconds (1800 = 30 minutes)
  sweep_interval: 1800

  # What to sweep
  sweep:
    metrics: true
    logs: true
    containers: true
    baseline_drift: true
    learning_consolidation: true
    # Tool-call iterations allowed per sweep phase. Sweeps are bounded
    # data-gathering, not open-ended chat — keep this small. Default 12.
    max_iterations: 12

  # Morning summary (TPS report style)
  morning_summary:
    enabled: true
    hour_start: 7   # Start checking at 7 AM
    hour_end: 9     # Stop checking at 9 AM

# Chat Interface
chat:
  enabled: true
  port: 8083
  websocket: true
  # Max chars of a single tool result appended to the LLM context. Oversized
  # output (kubectl dumps, Loki log floods) is clipped to the head plus a
  # marker, so one fat result can't inflate every later turn. Default 6000.
  max_tool_result_chars: 6000

# Event Runtime
event_runtime:
  scheduler:
    backend: json-file  # or apscheduler
    # jobstore_url: postgresql://user:password@host:5432/dbname  # optional; defaults to event runtime Postgres DSN, else local SQLite
    # spool_path: /var/lib/cfoperator/event-runtime/apscheduler-fired.jsonl
    misfire_grace_time_seconds: 300
  persistence:
    postgres:
      enabled: true
      table_name: event_runtime_events
      # dsn: ${CFOP_EVENT_RUNTIME_PG_DSN}  # optional override; defaults to top-level database config
  host_observability:
    enabled: true
    refresh_interval_seconds: 300
    default_to_local: true
    include_discovered_targets: true
    providers:
      - type: local
      # - type: ssh
      #   hosts:
      #     edge-01:
      #       address: 10.0.0.10
      #       ssh:
      #         user: aachten
      #         key_path: ${HOME}/.ssh/id_ed25519
      # - type: prometheus
      #   url: http://prometheus:9090
      #   discover: true
      #   job_pattern: node-exporter|node_exporter
      # - type: k3s
      #   url: http://prometheus:9090
      #   discover: true        # auto-discover nodes via kube_node_info
      #   timeout: 10           # Prometheus query timeout in seconds
  # Deep-investigation tier: host-shaped escalate / low-confidence verdicts
  # launch an ephemeral k8s Job running headless Claude Code that SSHes into
  # the affected host (journalctl, dmesg, disk health) and posts its report
  # back through the normal completion path. Read-only by design; proposed
  # fixes flow through the existing remediation PR gates.
  deep_investigation:
    enabled: false                      # CFOP_DEEP_INVESTIGATION_ENABLED
    image: ghcr.io/aachtenberg/cfoperator-worker:main   # CFOP_DEEP_WORKER_IMAGE
    namespace: apps                     # CFOP_DEEP_JOB_NAMESPACE
    service_account: cfoperator-worker  # read-only SA the Job runs as
    secrets_name: cfoperator-secrets    # holds ANTHROPIC_API_KEY + completion secret
    ssh_secret_name: cfop-forensics-ssh # dedicated forensics SSH keypair
    ssh_user: aachten
    # Where the Job posts its report back (event runtime service URL).
    # Required — the tier stays off without it. CFOP_DEEP_COMPLETION_BASE_URL
    completion_base_url: ""
    # agent_url: ""                     # defaults to CFOP_AGENT_URL (KB ingest)
    max_concurrent: 2                   # CFOP_DEEP_MAX_CONCURRENT
    daily_budget: 10                    # CFOP_DEEP_DAILY_BUDGET (launches/day)
    active_deadline_seconds: 900        # hard Job kill
    claude_timeout_seconds: 600         # claude -p timeout (margin for post-back)
    ttl_seconds_after_finished: 21600   # finished Jobs visible for 6h
    default_template: host-forensics    # worker prompt template
    default_model: claude-opus-4-8      # CFOP_DEEP_MODEL (haiku/sonnet = cost knob)
    routing:
      confidence_threshold: 0.4         # CFOP_DEEP_CONFIDENCE_THRESHOLD
      route_escalate: true              # host-shaped escalate -> deep_investigate
      route_low_confidence_investigate: true
      escalate_fallback_action: notify  # non-host escalate verdicts
      # Unclean-reboot alerts (details.boot_forensics, posted by the on-host
      # oneshot) skip triage and go straight to the worker with the
      # boot-forensics template. CFOP_DEEP_ROUTE_BOOT_FORENSICS
      route_boot_forensics: true

# Git & GitHub Integration
# Maps repositories to infrastructure targets so the agent can correlate
# code changes with alerts and investigate recent deployments.
#
# The console manages this list too: Admin -> Repos adds, edits and removes
# linked repos live (no restart, and in k8s no ConfigMap edit). A list saved
# there is stored in the database and REPLACES this block for the running
# system — the tab says which source is live, names any entry here that it is
# shadowing, and offers a revert. Precedence:
#   CFOP_GIT_REPOS_JSON (event runtime only) > the console list > this file.
# The agent applies a console change immediately; the event runtime resolves
# the list at startup, so its commit enrichment picks one up on its next
# restart.
git:
  github:
    token: ${GITHUB_TOKEN}
    # api_url: https://api.github.com  # override for GitHub Enterprise
  repos:
    - name: my-manifests
      github: my-org/my-manifests
      branch: main
      # path: ~/repos/my-manifests  # optional: local clone for git blame/diff; GitHub API is the default
      # ssh:                    # optional: for remote git access when using path on another host
      #   user: sre
      #   address: 10.0.0.5
      #   key_path: ${HOME}/.ssh/id_ed25519
      #   known_hosts_path: ${HOME}/.ssh/known_hosts  # enables StrictHostKeyChecking=yes
      hosts:
        - worker-1
      services:
        - apps
        - monitoring

# Remediation (Phase B) — turn a verified needs_action investigation into a
# concrete fix proposal. See docs/remediation-pipeline.md.
#
# Defaults OFF, and `profile: investigate` forces every flag here off regardless
# of what is written.
#
# With `enabled: true` alone the agent DRY-RUNS: it attaches a patch candidate
# or a precise decline reason to the investigation result and stops there.
#
# With `open_prs: true` it opens a real pull request against the manifest repo
# below. It still never touches the running cluster — the merge button is the
# only thing that deploys, and that stays a human's. Bounded by
# `max_open_prs` so a bad night cannot flood the repo.
remediation:
  enabled: false
  open_prs: false          # open a PR on the manifest repo (human merges it)
  deep_open_prs: false     # deep-investigation diffs -> PRs (same gates + shared cap)
  default_repo: my-manifests    # which `git.repos[].name` owns manifests to patch
  max_open_prs: 3
  # Queue / executor flags. Also toggleable live from the operator console,
  # which stores them in the database — the database wins over this file, and
  # `profile: investigate` wins over both.
  queue_feed: false
  queue_drain: false
  queue_reap: false
  queue_verify: false
  # The mutation judge (CFOP-70). Before a remediation that would auto-execute
  # is enqueued, a FRONTIER model is asked whether the change should be made
  # unattended at all — a different question from the one the classifier
  # answers, and the one nothing was asking when the pipeline proposed
  # un-pinning a deployment from the node it deliberately runs on.
  #
  # These are peers, tried in order, and each is pinned to its vendor's
  # frontier model in code (_JUDGE_MODEL_FLOOR): there is no config key that
  # can lower the model holding the veto, the same rule node-action follows.
  # A backend with no API key present is skipped, so listing all three costs
  # nothing and means one vendor outage does not park every remediation.
  #
  # Omit the block entirely to get all three in this order. Anything not in
  # {anthropic, xai, gemini} is dropped with a warning, never treated as a new
  # frontier tier.
  judge:
    providers: [anthropic, xai, gemini]

# Incident cockpit — the session `cfassist attach <id> --spawn` launches, in a
# pod or on a host outside the cluster (docs/cockpit.md). Every key is
# optional; the defaults below are what the spawner uses when this block is
# absent. The corresponding CFOP_COCKPIT_* env vars win over the file, because
# they are set by the same manifest that sets the image tag.
cockpit:
  # --- tier 1: the ephemeral pod ------------------------------------------
  namespace: apps
  image: ghcr.io/aachtenberg/cfoperator-cockpit:main
  service_account: cfoperator-cockpit   # read-only; needs the chart's cockpit.enabled
  agent_url: http://cfoperator.apps.svc.cluster.local:8083   # what the POD calls
  ttl_seconds: 14400          # activeDeadlineSeconds — the session, and its token
  ttl_after_finished_seconds: 3600
  # Per-runtime, not fleet-wide: two cockpits in the cluster, and two on each
  # host. The host half is what bounds how many session tokens can be sitting
  # on a machine that has no cluster-side ceiling above it.
  max_concurrent: 2
  # The model the in-pod session talks to. Defaults to the agent's own
  # llm.primary.url / llm.primary.model (where the loader puts the flat llm.url
  # and llm.model keys), so the cockpit and the investigation share a model.
  # It has to be an address the FLEET can reach — a loopback URL means "the
  # machine the session runs on", which is not this one.
  # llm_url: http://ollama:11434
  # llm_model: gemma4:26b

  # --- tiers 2/3: hosts outside the cluster (CFOP-36) ----------------------
  # Reached by ssh from the agent, so unlike tier 1 these need a credential in
  # the agent pod. Hosts themselves come from infrastructure.hosts (above) —
  # the same inventory the SSH tools and the host sweep use.
  #
  # The agent URL the SESSION calls, which is not agent_url: that one is
  # cluster DNS by design and a Pi cannot resolve it. Unset here means
  # agent_url is used, and a spawn onto a host is REFUSED (with this key
  # named) rather than producing a session that cannot fetch its briefing.
  # host_agent_url: http://10.0.0.14:8083
  #
  # Where the chart mounts the ssh secret, and the login to use for hosts
  # whose infrastructure.hosts entry does not set one. The directory is a
  # staging point, not ~/.ssh: a secret volume is root-owned and
  # group-readable, which ssh refuses for a private key, so the agent copies
  # it to ~/.ssh at 0600 on first use.
  # ssh_secret_dir: /cockpit-ssh
  # ssh_user: sre
  # ssh_key_path: ''          # explicit -i, when the staged default is wrong
  # ssh_connect_timeout: 5
  # ssh_command_timeout: 30
  #
  # How long a capability probe is trusted. Successes only: a probe FAILURE is
  # cached for about a connect timeout, so fixing the key or the route works on
  # the next attempt rather than fifteen minutes later.
  # probe_cache_seconds: 900
  #
  # The janitor sweep — removes cockpit containers and /tmp sessions whose
  # recorded expiry has passed. The `cockpit_reap_interval` console setting
  # (seconds) wins over this, so it can be changed without a redeploy.
  # janitor_interval_seconds: 900
  #
  # Let tier 3 create its self-destruct timer through `sudo -n systemd-run` on
  # a host with no user systemd manager. Off by default: without it such a host
  # degrades to the ssh tier and the janitor carries the cleanup alone.
  # allow_sudo: false
  #
  # The cfassist release tiers 3/3b deliver to the host. Pinned so a session is
  # reproducible; it must be a tag that exists, and a spawn says so loudly if
  # it does not. Defaults to cfassist-go's own Version.
  # cfassist_version: 0.10.2
  # release_base: https://github.com/aachtenberg/cfoperator/releases/download
  #
  # ---- the browser bridge (CFOP-75) ----------------------------------------
  #
  # Lets the console open a terminal on a host-tier cockpit. Its own listener
  # rather than a path on :8083, because the console runs under Waitress, which
  # cannot upgrade a connection. Off by default: this is the sharpest port the
  # agent can open, and it should exist only where someone asked for it.
  # bridge_enabled: false
  # bridge_port: 8084
  # bridge_bind: 0.0.0.0
  #
  # Which pages may open a terminal. There is NO default and no wildcard: with
  # this unset the bridge refuses to listen at all rather than starting up and
  # rejecting every connection. Set it to the console's own origin, exactly as
  # it appears in the address bar — scheme, host and port; a trailing slash and
  # letter case are both forgiven.
  # bridge_origins: http://10.0.0.14:8083
  #
  # A connection needs an `investigate`-scoped token, the same one
  # `cfassist attach` mints for an interactive session, and the cockpit it asks
  # for has to already exist — the bridge attaches, it never spawns. Tier 1
  # (`pod`) is refused by name: that needs pods/attach, which nothing here
  # holds, and granting it is CFOP-59 Phase B.
  #
  # :8083 is guarded at the host level rather than by a NetworkPolicy (the pod
  # is hostNetwork, so netpol is unenforceable). This port needs the same
  # treatment — see DEPLOYMENT.md.

# Skills
skills:
  directory: ./skills
  enabled:
    - investigate-container
    - investigate-code-change
    - why-restart
    - compare-hosts
```
