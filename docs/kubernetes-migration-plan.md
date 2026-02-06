# Infrastructure Configuration Guide

## Overview

CFOperator is designed to monitor **heterogeneous infrastructure** - not just Docker containers on one host, but your entire fleet:

- 🖥️ **Bare metal servers** (Raspberry Pis, x86 servers)
- 🐳 **Docker containers** (any host running Docker)
- ☸️ **Kubernetes pods** (if you add clusters)
- 💾 **Databases** (PostgreSQL, InfluxDB, Redis, etc.)
- 🌐 **Applications** (Immich, Home Assistant, custom services)
- 📊 **Any system with metrics/logs** (Prometheus, Loki, Datadog, Dynatrace)

## Core Principle: Observability-First

CFOperator doesn't need direct access to **run** your infrastructure.
It needs access to **observe** your infrastructure:

1. **Metrics APIs** - Prometheus, Datadog, Dynatrace, VictoriaMetrics
2. **Log APIs** - Loki, Elasticsearch, Datadog Logs, CloudWatch
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
      address: 192.168.0.167
      role: primary
      ssh:
        user: aachten
        key_path: ~/.ssh/id_rsa
      monitoring:
        - prometheus       # Scrapes metrics
        - loki            # Collects logs
        - docker          # Runs containers

    # Worker hosts
    raspberrypi2:
      address: 192.168.0.146
      role: worker
      ssh:
        user: aachten
        key_path: ~/.ssh/id_rsa
      monitoring:
        - node_exporter   # Exports metrics
        - promtail        # Ships logs
        - docker          # Runs containers

    raspberrypi3:
      address: 192.168.0.111
      role: worker
      ssh:
        user: aachten
        key_path: ~/.ssh/id_rsa
      monitoring:
        - node_exporter
        - promtail
        - docker

    # Add more hosts as needed
    # nas-server:
    #   address: 192.168.0.200
    #   role: storage
    #   ssh: {...}
```

### SSH Requirements

**CRITICAL: SSH must be passwordless and sudo must be passwordless.**

#### Setup SSH Key Authentication

On the CFOperator host (raspberrypi):

```bash
# Generate SSH key if you don't have one
ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa

# Copy to each worker host
ssh-copy-id aachten@192.168.0.146
ssh-copy-id aachten@192.168.0.111
ssh-copy-id aachten@192.168.0.116

# Test passwordless login
ssh aachten@192.168.0.146 'echo "Success"'
```

#### Setup Passwordless Sudo

On each worker host (Pi2, Pi3, Pi4):

```bash
# Add your user to sudoers with NOPASSWD
echo "aachten ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/aachten

# Test
sudo ls  # Should not ask for password
```

**Why this matters:**
CFOperator may need to restart services (`sudo systemctl restart docker`) during investigations. If sudo prompts for a password, the automation breaks.

### Observability Backend Configuration

CFOperator connects to your existing observability infrastructure:

```yaml
observability:
  # Metrics - choose what you're using
  metrics:
    backend: prometheus  # or: victoria_metrics, datadog, dynatrace
    url: http://192.168.0.167:9090
    timeout: 30
    # For Datadog/Dynatrace:
    # api_key: ${DATADOG_API_KEY}
    # site: us5.datadoghq.com

  # Logs - choose what you're using
  logs:
    backend: loki  # or: elasticsearch, datadog, splunk
    url: http://192.168.0.167:3100
    timeout: 30

  # Containers - multiple backends possible
  containers:
    backend: docker  # or: kubernetes, podman
    hosts:
      local: unix:///var/run/docker.sock
      # If remote Docker APIs are exposed:
      # pi2: tcp://192.168.0.146:2375
      # pi3: tcp://192.168.0.111:2375

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
   - raspberrypi2 is at 192.168.0.146
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

## Pluggable Architecture

CFOperator's observability backends are **pluggable**. You can swap implementations without changing the agent code:

### Example: Switching from Prometheus to Datadog

**Before (Prometheus):**
```yaml
observability:
  metrics:
    backend: prometheus
    url: http://192.168.0.167:9090
```

**After (Datadog):**
```yaml
observability:
  metrics:
    backend: datadog
    api_key: ${DATADOG_API_KEY}
    site: us5.datadoghq.com
```

CFOperator's LLM doesn't care - it still calls `query_metric("cpu_usage{host=pi2}")` and the backend adapter translates to Datadog's query language.

### Example: Adding Dynatrace

Create `observability/dynatrace.py`:

```python
class DynatraceMetrics(MetricsBackend):
    def query(self, query: str) -> Dict[str, Any]:
        # Translate PromQL to Dynatrace DQL
        # Call Dynatrace API
        # Return normalized result
        pass
```

Update `config.yaml`:
```yaml
observability:
  metrics:
    backend: dynatrace
    api_token: ${DYNATRACE_API_TOKEN}
    environment: abc12345.live.dynatrace.com
```

## Example: Multi-Cloud Infrastructure

CFOperator can monitor hybrid infrastructure:

```yaml
infrastructure:
  hosts:
    # On-premises
    raspberrypi:
      address: 192.168.0.167
      role: primary
      ssh: {...}

    # AWS EC2
    app-server-1:
      address: 10.0.1.50  # VPN/private IP
      role: application
      ssh:
        user: ubuntu
        key_path: ~/.ssh/aws-key.pem
      monitoring:
        - cloudwatch
        - docker

    # GCP VM
    db-server-1:
      address: 10.1.0.20
      role: database
      ssh:
        user: admin
        key_path: ~/.ssh/gcp-key
      monitoring:
        - stackdriver
        - postgresql

observability:
  metrics:
    # Aggregate from multiple sources
    backends:
      - type: prometheus
        url: http://192.168.0.167:9090
        scope: homelab
      - type: cloudwatch
        region: us-east-1
        scope: aws
      - type: stackdriver
        project: my-gcp-project
        scope: gcp
```

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
    ssh aachten@$host 'echo "OK" && sudo echo "SUDO OK"'
done

# Restart CFOperator
cd ~/cfoperator
docker compose restart

# Watch logs
docker logs -f cfoperator
```

## Troubleshooting

### SSH connection refused

```
Error: connection refused to 192.168.0.146:22
```

Fix: Ensure SSH is running and firewall allows port 22:
```bash
ssh aachten@192.168.0.146
sudo systemctl status sshd
sudo ufw allow 22
```

### Sudo password prompt

```
Error: sudo: a password is required
```

Fix: Add NOPASSWD to sudoers:
```bash
ssh aachten@192.168.0.146
echo "aachten ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/aachten
```

### Host not in config

```
Error: Unknown host: raspberrypi5
```

Fix: Add to config.yaml and restart CFOperator.
