# Infrastructure Configuration Guide

## Overview

CFOperator is designed to monitor **heterogeneous infrastructure** - not just Docker containers on one host, but your entire fleet:

- 🖥️ **Bare metal servers** (Raspberry Pis, x86 servers)
- 🐳 **Docker containers** (any host running Docker)
- ☸️ **Kubernetes pods**
- 💾 **Databases** (anything exporting metrics Prometheus can scrape)
- 🌐 **Applications** (any service with metrics or logs)

## What actually ships

This is the whole list. A backend not in the **Shipped** column does not exist
in this build. A `config.yaml` naming one is not rejected, which makes a typo
easy to miss: the agent logs `Unsupported metrics backend: <name>` (or logs,
containers, alerts) at startup and runs with that backend switched off. Treat
the other two columns as intent, not as configuration you can write today.

The shipped metrics/logs/containers/alerts backends are exactly the names the
agent accepts in `_init_observability_backends` (`agent/agent.py`), and
`tests/test_docs_extension.py` holds this table to that list. Notifications
are wired in two places, both reading the one `observability.notifications`
list. The agent sends to `slack` and `discord` (and can post to Alertmanager).
The event runtime has its own sinks for `slack`, `discord` and `ntfy`, and
`ntfy` exists only there; each process skips an entry it has no sink for. If
this table and the code ever disagree, the code is right and this table is a
bug.

| Capability | Shipped | Planned | Not planned |
|---|---|---|---|
| Metrics | `prometheus` | VictoriaMetrics (Prometheus-compatible, likely small) | Datadog, Dynatrace, New Relic |
| Logs | `loki` | — | Elasticsearch, Splunk, CloudWatch |
| Containers | `kubernetes`, `docker`, `prometheus` (bare-metal discovery) | — | Nomad, ECS |
| Alerts (ingest) | `alertmanager` | — | PagerDuty, Opsgenie |
| Notifications (out) | `slack`, `discord`, `ntfy` | — | PagerDuty, Opsgenie, email |
| Issue trackers (out) | `plane`, `github` (Issues) | `jira` (adapter shipped, **not live-tested**) | Linear, ServiceNow |

Dynatrace problems can still reach the event runtime through an optional plugin,
`integrations/dynatrace`, loaded only when named in `CFOP_EVENT_RUNTIME_PLUGINS`
([event-runtime-quickstart.md](event-runtime-quickstart.md#dynatrace-problems-integrationsdynatrace)).
It is an alert source, not a metrics or logs backend, so the table above stands.

**Why so short?** The design bet is that Prometheus + Loki + Alertmanager covers
the target user — a self-hosted shop that will not send its logs to a SaaS. The
"not planned" column is not a backlog: those are the vendors whose customers are
already served by that vendor's own AI features. If you need one, open an
*integration request* issue — that is the demand signal that decides whether it
gets built, and writing your own is documented below.

## Core Principle: Observability-First

CFOperator doesn't need direct access to **run** your infrastructure.
It needs access to **observe** your infrastructure:

1. **Metrics APIs** - Prometheus
2. **Log APIs** - Loki
3. **Container APIs** - Docker API, Kubernetes API
4. **SSH access** - For troubleshooting when observability isn't enough

## Configuration Approach

For config *mechanics* — how the file is merged over defaults, what `profile:`
does, and the full annotated option list — see
[config-reference.md](config-reference.md). This section is about describing a
fleet.

### Infrastructure Hosts

**Optional.** CFOperator discovers workloads from the Kubernetes API and from
Prometheus targets at runtime, so a getting-started config declares no hosts at
all. A static inventory is an overlay that unlocks the hybrid-fleet tier: the
SSH and host-discovery tools, and baseline drift against expected services.
Leave it out and those tools log that they are disabled; nothing else changes.

Define your fleet in `config.yaml`:

```yaml
infrastructure:
  hosts:
    # Primary host (runs CFOperator itself)
    raspberrypi:
      address: 10.0.0.1
      role: primary
      ssh:
        user: user
        key_path: ~/.ssh/id_rsa
      monitoring:
        - prometheus       # Scrapes metrics
        - loki            # Collects logs
        - docker          # Runs containers
      # What baseline drift expects to find running here. Each entry names a
      # container the sweep looks for in the Docker or Prometheus container
      # backend's listing, and a missing one is a finding.
      services:
        - name: grafana
          container: grafana

    # Worker hosts
    raspberrypi2:
      address: 10.0.0.2
      role: worker
      ssh:
        user: user
        key_path: ~/.ssh/id_rsa
      monitoring:
        - node_exporter   # Exports metrics
        - promtail        # Ships logs
        - docker          # Runs containers

    raspberrypi3:
      address: 10.0.0.3
      role: worker
      ssh:
        user: user
        key_path: ~/.ssh/id_rsa
      monitoring:
        - node_exporter
        - promtail
        - docker

    ollama-gpu:
      address: 10.0.0.5
      role: gpu
      ssh:
        user: user
        key_path: ~/.ssh/id_rsa
      monitoring:
        - node_exporter
        - promtail
        - docker
```

### SSH Requirements

**SSH must be key-based (no password). Sudo must be passwordless for a short
allowlist of reads, and need not be passwordless for anything else.**

#### Setup SSH Key Authentication

On the CFOperator host (raspberrypi in the example above):

```bash
# Generate SSH key if you don't have one
ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa

# Copy to each worker host
ssh-copy-id user@10.0.0.2
ssh-copy-id user@10.0.0.3
ssh-copy-id user@10.0.0.5

# Test passwordless login
ssh user@10.0.0.2 'echo "Success"'
```

#### Setup Passwordless Sudo, for reads only

Investigations read with sudo (`sudo dmesg`, `sudo crictl ps`), so the agent's
SSH user needs those commands without a password. It does not need, and
should not have, `NOPASSWD: ALL`. Investigations never change hosts: every
unattended run is read-only, and each command it sends is classified before it
runs (CFOP-240). That classifier is a denylist, though: a script or an
interpreter one-liner is not recognised. With `NOPASSWD: ALL` it would be the
only thing between an investigation and root. A sudoers drop-in that lists
the reads makes the host enforce the boundary:

```bash
# /etc/sudoers.d/cfop-reader -- validate with: sudo visudo -cf <file>
cfop-reader ALL=(root) NOPASSWD: /usr/bin/dmesg -T, /usr/bin/du *, \
    /usr/local/bin/crictl ps, /usr/local/bin/crictl ps *, /usr/bin/docker ps, /usr/bin/docker logs *
```

List exact argument forms where an argument could change what a command does:
a command listed with no arguments may be run with ANY arguments, so a bare
`/usr/bin/dmesg` would admit `dmesg -C`. The homelab's full list and its
reasoning are in homelab-infra `ansible/files/sudoers-cfop-forensics`.

The node-action lane, which runs the gated host fixes, uses its own identity
with its own drop-in, scoped to the `node_action` allowlist.

`discover_all_hosts` checks this the way it is meant to be set up. It *runs*
each probe read with `sudo -n`, output discarded and capped at 5s, and
reports `sudo_reads` (every probe ran) plus a host issue naming any probe that
was refused. Running the read matters: `sudo -n -l <cmd>` succeeds for an
entry that would still prompt for a password, under sudo's default
`listpw=any`. Full sudo (`sudo_passwordless`) is reported but is not an issue.
The probes and the number of hosts checked at once are configurable:

```yaml
infrastructure:
  discovery:
    # Reads your drop-in allows. They are EXECUTED as root, so list only
    # harmless reads; absolute path and plain arguments only.
    sudo_read_probes:
      - /usr/bin/dmesg -T
    workers: 16          # hosts checked at once (1-64)
```

Watch for `sudo-rs` (Ubuntu 25.10 and later). It resolves symlinks before
matching sudoers. `/usr/bin/du` there is a symlink into uutils coreutils, and
k3s's `kubectl` and `crictl` are symlinks to `k3s`, so an entry for the
symlink's path never matches. A probe for such a command is how you find out.

### Observability Backend Configuration

CFOperator connects to your existing observability infrastructure. Every key
and its default is in [config-reference.md](config-reference.md#the-full-config);
this is the shape for a fleet like the one above:

```yaml
observability:
  # Metrics. Shipped: prometheus
  metrics:
    backend: prometheus
    url: http://10.0.0.1:9090

  # Logs. Shipped: loki
  logs:
    backend: loki
    url: http://10.0.0.1:3100

  # Containers - a list, one entry per backend.
  # Shipped: kubernetes, docker, prometheus (bare-metal discovery)
  containers:
    - backend: docker
      hosts:
        local: unix:///var/run/docker.sock
        # If remote Docker APIs are exposed:
        # node-2: tcp://10.0.0.2:2375
        # node-3: tcp://10.0.0.3:2375
    # - backend: kubernetes     # if you have a cluster

# kubectl settings, top level rather than under observability. One context,
# not a list; unset, kubectl's own default applies.
# kubernetes:
#   kubeconfig: ~/.kube/config
#   context: homelab-cluster
```

## Discovery: How CFOperator Learns Your Infrastructure

### 1. Static Configuration (config.yaml)

`infrastructure.hosts` names hosts explicitly. It is optional, and it is what
the SSH tools, host discovery and baseline drift act on: a host the inventory
does not name cannot be reached over SSH.

### 2. Dynamic Discovery (Prometheus)

The event runtime's `prometheus` host provider
(`event_runtime.host_observability.providers`) finds hosts from node-exporter
targets, querying `up{job=~"node-exporter|node_exporter"} == 1` (the
`job_pattern` is configurable). The `k3s` provider finds nodes from
`kube_node_info`. That gives triage which hosts are monitored and whether each
is up, with no inventory.

### 3. Container Discovery

Each configured container backend lists containers its own way:

1. **Docker API** (`docker`) - the containers on each host in its `hosts` map
2. **Kubernetes API** (`kubernetes`) - pods, through kubectl
3. **Prometheus metrics** (`prometheus`) - Telegraf's
   `docker_container_status_uptime_ns`, with logs fetched over SSH

### 4. Services

There is no service-discovery pass. An investigation looks services up as it
needs them: the Kubernetes tools list Services and Ingresses, and on a host in
the inventory the model can run `systemctl` over SSH. Nothing parses
`docker-compose.yml` files.

## How CFOperator Uses This Information

### Reactive Mode (Alert-Driven)

1. **Alert fires**: "immich-ml container OOM killed on raspberrypi2"
2. **Orient**: CFOperator knows:
   - raspberrypi2 is at 10.0.0.2
   - SSH access available
   - Docker API available
   - Prometheus metrics available
3. **Act**:
   - Query Prometheus: `container_memory_usage_bytes{name="immich-ml"}`
   - SSH to Pi2: `docker logs immich-ml --tail 100`
   - Check disk: `df -h /var/lib/docker`
4. **Propose, never apply**: an investigation is read-only, and CFOperator
   changes nothing unattended. A fix it recommends becomes a remediation with
   a named human behind it: the merge of its GitOps PR, the merge of the
   change-record PR a host action opens, or an admin confirming the command
   in the console ([REMEDIATION.md](REMEDIATION.md)).

### Proactive Mode (Deep Sweeps)

Every 30 minutes by default (`ooda.sweep_interval`), CFOperator:

1. **Sweeps in phases**, each switchable under `ooda.sweep`: metrics (PromQL
   against the metrics backend), logs, containers (every container backend),
   baseline drift (the inventory's `services` against what is running), and
   learning consolidation. None of it needs an inventory except baseline
   drift.

2. **Correlates across fleet**:
   - "Pi2 and Pi3 both restarted at same time" → power issue?
   - "All containers using PostgreSQL slow" → database bottleneck?
   - "Disk filling on Pi4 at 5%/week" → will hit 90% in 2 weeks

3. **Learns patterns**:
   - "immich-ml always OOMs after Pi2 reboots" → increase memory limit
   - "influxdb writes fail when loki is busy" → I/O contention

## Extending CFOperator

There are two layers, and only one of them takes plugins.

### The event runtime takes plugins

The event runtime (alert intake, triage, dispatch to the agent, notification)
imports the modules named in `CFOP_EVENT_RUNTIME_PLUGINS` and lets each one
register:

- **alert sources**: somewhere new that alerts come from;
- **context providers**: enrichment before triage, including **evidence** that
  the investigation itself is shown;
- **completion observers**: acting on an investigation's result, for example
  writing it back to the system the alert came from;
- **notification sinks** and **action handlers**.

No core change is needed. The contract, a minimal example, and the rules for
evidence and observers are in
[event-runtime-quickstart.md](event-runtime-quickstart.md#external-plugins).
`integrations/dynatrace/` is a complete worked example, for a vendor that is
"not planned" as a backend above. It turns Davis problems into alerts, hands
each investigation fixed-DQL evidence, and writes the conclusion back onto the
problem.

### The agent's backends do not

Replacing the metrics or logs backend (Prometheus, Loki) with another vendor is a
code change in the agent, not a plugin:

- The backend names are a fixed list in `_init_observability_backends`
  (`agent/agent.py`). Any other name is logged as unsupported and switched off.
- The investigation tools `prometheus_query` and `loki_query`
  (`tools/__init__.py`) take PromQL and LogQL that the model writes, and they
  correct it as PromQL and LogQL.
- The agent's sweeps send hard-coded PromQL to the metrics backend.

An adapter implementing `MetricsBackend` for another vendor would therefore
have to accept arbitrary PromQL. A store that speaks the Prometheus query API
avoids all of this. VictoriaMetrics, the one planned entry, is expected to work
as `backend: prometheus` with its URL. That has not been tested, which is why
it is listed as planned rather than shipped.

If you need another vendor, open an integration request issue. Alert-shaped
integrations (problems, incidents) are plugin-sized, like the Dynatrace one.
A metrics or logs backend is core work.

## Hybrid and multi-site infrastructure

Hosts can live anywhere you can reach — on-prem, a VPS, a cloud VM — as long as
the metrics land in the Prometheus you point CFOperator at, and SSH (if you want
the troubleshooting path) reaches them.

```yaml
infrastructure:
  hosts:
    # On-premises
    primary:
      address: 10.0.0.1
      role: primary
      ssh: {...}

    # Cloud VM, reachable over VPN/private networking
    app-server-1:
      address: 10.0.1.50
      role: application
      ssh:
        user: ubuntu
        key_path: ~/.ssh/cloud-key.pem
      monitoring:
        - docker
```

**Note the limitation honestly:** there is one metrics backend and one logs
backend per deployment. Aggregating several metrics sources under one agent
(a `backends:` *list*) is **not implemented** — if you need on-prem and a cloud
provider's native monitoring together, today you point CFOperator at whichever
Prometheus sees both, or run an agent per site.

## Next Steps

1. **Verify SSH access** to all hosts
2. **Allow the sudo reads** (above) without a password on all hosts
3. **Test observability backends** (Prometheus, Loki, etc.)
4. **Add hosts to config.yaml**. The compose stack mounts
   `deploy/compose/config.yaml`; to keep your edit across upgrades, mount your
   own file from `docker-compose.override.yml`
   ([DEPLOYMENT.md](DEPLOYMENT.md#local--non-production))
5. **Restart the agent** to pick up the new configuration: config is read at
   start
6. **Monitor logs** to see host discovery

```bash
# Check SSH access and one allowlisted read
for host in raspberrypi2 raspberrypi3 ollama-gpu; do
    echo "Testing $host..."
    ssh user@$host 'echo "OK" && sudo -n /usr/bin/dmesg -T >/dev/null && echo "SUDO READ OK"'
done

# From the directory holding docker-compose.yml (~/cfoperator for an
# installer stack): restart the agent and watch it
docker compose restart agent
docker compose logs -f agent
```

## Troubleshooting

### SSH connection refused

```
Error: connection refused to 10.0.0.2:22
```

Fix: Ensure SSH is running and firewall allows port 22:
```bash
ssh user@10.0.0.2
sudo systemctl status sshd
sudo ufw allow 22
```

### Sudo password prompt

```
Error: sudo: a password is required
```

Fix: add the read that prompted to the host's sudoers drop-in (see
[Setup Passwordless Sudo, for reads only](#setup-passwordless-sudo-for-reads-only)),
not `NOPASSWD: ALL`, then validate it:
```bash
ssh user@10.0.0.2
sudo visudo -cf /etc/sudoers.d/cfop-reader
```

### Host not in config

```
Error: Unknown host: raspberrypi5
```

Fix: Add to config.yaml and restart CFOperator.
