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
in this build, and a `config.yaml` naming it fails at startup — so treat the
other two columns as intent, not as configuration you can write today.

The shipped metrics/logs/containers/alerts backends are exactly what
`observability/__init__.py` registers. Outbound notification sinks are wired in
`event_runtime` instead (that is where `ntfy` lives). If this table and the code
ever disagree, the code is right and this table is a bug.

| Capability | Shipped | Planned | Not planned |
|---|---|---|---|
| Metrics | `prometheus` | VictoriaMetrics (Prometheus-compatible, likely small) | Datadog, Dynatrace, New Relic |
| Logs | `loki` | — | Elasticsearch, Splunk, CloudWatch |
| Containers | `kubernetes`, `docker`, `prometheus` (bare-metal discovery) | — | Nomad, ECS |
| Alerts (ingest) | `alertmanager` | — | PagerDuty, Opsgenie |
| Notifications (out) | `slack`, `discord`, `ntfy` | — | PagerDuty, Opsgenie, email |

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

### Infrastructure Hosts

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

**CRITICAL: SSH must be passwordless and sudo must be passwordless.**

#### Setup SSH Key Authentication

On the CFOperator host (raspberrypi3):

```bash
# Generate SSH key if you don't have one
ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa

# Copy to each worker host
ssh-copy-id user@10.0.0.2
ssh-copy-id user@10.0.0.3
ssh-copy-id user@10.0.0.4

# Test passwordless login
ssh user@10.0.0.2 'echo "Success"'
```

#### Setup Passwordless Sudo

On each worker host (Pi2, Pi3, Pi4):

```bash
# Add your user to sudoers with NOPASSWD
echo "user ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/user

# Test
sudo ls  # Should not ask for password
```

**Why this matters:**
CFOperator may need to restart services (`sudo systemctl restart docker`) during investigations. If sudo prompts for a password, the automation breaks.

### Observability Backend Configuration

CFOperator connects to your existing observability infrastructure:

```yaml
observability:
  # Metrics. Shipped: prometheus
  metrics:
    backend: prometheus
    url: http://10.0.0.1:9090
    timeout: 30

  # Logs. Shipped: loki
  logs:
    backend: loki
    url: http://10.0.0.1:3100
    timeout: 30

  # Containers - multiple backends possible.
  # Shipped: kubernetes, docker, prometheus (bare-metal discovery)
  containers:
    backend: docker
    hosts:
      local: unix:///var/run/docker.sock
      # If remote Docker APIs are exposed:
      # node-2: tcp://10.0.0.2:2375
      # node-3: tcp://10.0.0.3:2375

  # Kubernetes (if you have a cluster)
  # kubernetes:
  #   kubeconfig: ~/.kube/config
  #   contexts:
  #     - homelab-cluster
```

## Discovery: How CFOperator Learns Your Infrastructure

### 1. Static Configuration (config.yaml)

You explicitly define hosts in `infrastructure.hosts`. This is the **source of truth**.

### 2. Dynamic Discovery (Prometheus Service Discovery)

If you use Prometheus, CFOperator can discover hosts automatically:

```python
# Query Prometheus for all monitored targets
targets = prometheus_query('up{job="node-exporter"}')
# Discovers: raspberrypi, raspberrypi2, raspberrypi3, raspberrypi4
```

This gives CFOperator:
- Which hosts are monitored
- Which exporters are running where
- Current up/down status

### 3. Container Discovery

CFOperator discovers containers by:

1. **Docker API** - List containers on each Docker host
2. **Kubernetes API** - List pods in each namespace
3. **Prometheus metrics** - Parse `container_*` metrics to find containers

Example:
```python
# CFOperator discovers:
- raspberrypi: 16 containers (influxdb, loki, prometheus, grafana, ...)
- raspberrypi2: 5 containers (immich-server, immich-ml, redis, postgres, ...)
- raspberrypi3: 3 containers (homeassistant, zigbee2mqtt, mosquitto, ...)
- raspberrypi4: 2 containers (pihole, unbound, ...)
```

### 4. Service Discovery

CFOperator learns about services from:
- **Systemd** - `systemctl list-units --type=service` via SSH
- **Docker Compose** - Parse `docker-compose.yml` files
- **Kubernetes** - Query Services and Ingresses

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
   - Restart if needed: `docker restart immich-ml`

### Proactive Mode (Deep Sweeps)

Every 30 minutes, CFOperator:

1. **Sweeps all hosts**:
   ```python
   for host in infrastructure.hosts:
       - Check system metrics (CPU, memory, disk)
       - List running containers/services
       - Compare to baseline
       - Look for anomalies
   ```

2. **Correlates across fleet**:
   - "Pi2 and Pi3 both restarted at same time" → power issue?
   - "All containers using PostgreSQL slow" → database bottleneck?
   - "Disk filling on Pi4 at 5%/week" → will hit 90% in 2 weeks

3. **Learns patterns**:
   - "immich-ml always OOMs after Pi2 reboots" → increase memory limit
   - "influxdb writes fail when loki is busy" → I/O contention

## Writing your own backend

The observability layer is an interface, not a fixed list. Nothing below ships —
this is the extension path if the shipped set does not cover you, and the
example is deliberately a vendor CFOperator has no plans to support, to make the
point that you do not need us to.

The agent never learns a vendor's query language. It calls
`query_metric("cpu_usage{host=pi2}")` and your adapter translates; that is the
whole contract.

**1. Implement the interface.** Create `observability/mymetrics.py`:

```python
from .base import MetricsBackend

class MyMetrics(MetricsBackend):
    def query(self, query: str) -> Dict[str, Any]:
        # Translate the PromQL-shaped query to your vendor's language,
        # call their API, and return the normalized result shape.
        ...
```

**2. Register it** in `observability/__init__.py` — import it and add it to
`__all__`. That file is the single source of truth for what the config loader
will accept, which is why the matrix at the top of this document is checkable
against it.

**3. Name it in config:**

```yaml
observability:
  metrics:
    backend: mymetrics
    api_token: ${MY_API_TOKEN}
```

If you build one that others would want, an integration request issue plus a PR
is the fastest route to it shipping.

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
2. **Ensure passwordless sudo** on all hosts
3. **Test observability backends** (Prometheus, Loki, etc.)
4. **Add hosts to config.yaml**
5. **Restart CFOperator** to pick up new configuration
6. **Monitor logs** to see host discovery

```bash
# Check SSH access
for host in raspberrypi2 raspberrypi3 raspberrypi4; do
    echo "Testing $host..."
    ssh user@$host 'echo "OK" && sudo echo "SUDO OK"'
done

# Restart CFOperator
cd ~/repos/cfoperator
docker compose restart

# Watch logs
docker logs -f cfoperator
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

Fix: Add NOPASSWD to sudoers:
```bash
ssh user@10.0.0.2
echo "user ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/user
```

### Host not in config

```
Error: Unknown host: raspberrypi5
```

Fix: Add to config.yaml and restart CFOperator.
