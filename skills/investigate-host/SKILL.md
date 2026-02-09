---
name: investigate-host
description: "Run a systematic investigation of a host's health, resource usage, services, and connectivity. Use this when a host is slow, unreachable, low on resources, or behaving unexpectedly. Works for bare metal servers, VMs, and Raspberry Pis — not just Docker hosts. Keywords: host, server, node, investigate, troubleshoot, disk, memory, cpu, load, ssh, systemd, reboot."
---

# Investigate Host

This skill performs a comprehensive investigation of a host machine, gathering diagnostics from metrics, SSH, and logs to identify issues at the OS/infrastructure level — independent of any specific container.

## When to Use

- Host is slow or unresponsive
- High CPU, memory, or disk usage alerts
- Host recently rebooted unexpectedly
- Services failing to start
- Network connectivity issues
- Investigating fleet-wide patterns affecting a specific node
- Capacity planning or health audit

## What It Does

The skill will:
1. Verify host reachability (ping + SSH)
2. Gather system info (OS, kernel, uptime, last reboot)
3. Check resource usage via Prometheus (CPU, memory, disk, swap, network)
4. Get top processes by CPU and memory (SSH)
5. List systemd services and flag failed units
6. List Docker containers (if Docker is running)
7. Check recent system logs for errors (journalctl)
8. Search past investigations and learnings for this host
9. Provide a summary with findings and recommendations

## Usage

Invoke: `/investigate-host <hostname>`

The hostname must match a host defined in `config.yaml` under `infrastructure.hosts`.

Examples:
- `/investigate-host raspberrypi` - Investigate the primary host
- `/investigate-host raspberrypi3` - Investigate a worker node
- `/investigate-host ollama-gpu` - Investigate the GPU server

## Implementation

### Phase 1: Connectivity Check

Verify the host is reachable before spending time on deeper diagnostics.

```
1. ping_host(hostname)
   - Confirm host is alive
   - Measure latency (flag if >50ms on local network)

2. verify_ssh(hostname)
   - Confirm SSH access works
   - If SSH fails, report and stop — remaining checks need SSH
```

### Phase 2: System Overview

Get the big picture of the host's current state.

```
3. ssh_get_system_info(hostname)
   - OS version, kernel, architecture
   - Uptime and last reboot time
   - Disk usage per mount point
   - Memory/swap summary
   - Load average

4. prometheus_query — Resource metrics (last 5m averages):
   a. CPU: 100 - (avg(rate(node_cpu_seconds_total{mode="idle", instance=~"<host>.*"}[5m])) * 100)
   b. Memory: (1 - node_memory_MemAvailable_bytes{instance=~"<host>.*"} / node_memory_MemTotal_bytes{instance=~"<host>.*"}) * 100
   c. Disk: (1 - node_filesystem_avail_bytes{instance=~"<host>.*", mountpoint="/"} / node_filesystem_size_bytes{instance=~"<host>.*", mountpoint="/"}) * 100
   d. Swap: node_memory_SwapTotal_bytes{instance=~"<host>.*"} - node_memory_SwapFree_bytes{instance=~"<host>.*"}
   e. Network errors: rate(node_network_receive_errs_total{instance=~"<host>.*"}[5m])
   f. Load: node_load1{instance=~"<host>.*"}, node_load5{instance=~"<host>.*"}, node_load15{instance=~"<host>.*"}
```

### Phase 3: Process and Service Analysis

Identify what's consuming resources and what's broken.

```
5. ssh_execute(hostname, "ps aux --sort=-%mem | head -11")
   - Top 10 processes by memory

6. ssh_execute(hostname, "ps aux --sort=-%cpu | head -11")
   - Top 10 processes by CPU

7. ssh_list_services(hostname)
   - All running Docker containers AND systemd services
   - Flag any failed or inactive services

8. ssh_execute(hostname, "systemctl --failed --no-pager")
   - Explicitly list failed systemd units
```

### Phase 4: Log Analysis

Check for recent errors and warnings.

```
9. ssh_execute(hostname, "journalctl --since '1 hour ago' --priority=err --no-pager -n 50")
   - Recent error-level system log entries
   - Look for: OOM kills, service failures, disk errors, network drops

10. ssh_execute(hostname, "dmesg --time-format iso | tail -30")
    - Recent kernel messages
    - Look for: hardware errors, OOM, I/O errors, USB disconnects
```

### Phase 5: Historical Context

Check if this host has had similar issues before.

```
11. find_learnings(query="host issues <hostname>", services=[], limit=5)
    - Past learnings about this host

12. find_learnings(query="<primary_symptom>", limit=3)
    - Learnings matching the main symptom found (e.g., "high memory", "disk full")
```

### Phase 6: Summary

Synthesize all findings into an actionable report.

```
13. Generate summary with:
    - Host identity and role
    - Current state (healthy / degraded / critical)
    - Resource usage snapshot (with thresholds)
    - Key findings (sorted by severity)
    - Failed services or containers
    - Recent errors from logs
    - Past learnings that may apply
    - Recommended actions
```

## Severity Thresholds

| Metric | Warning | Critical |
|--------|---------|----------|
| CPU usage | >70% | >90% |
| Memory usage | >80% | >95% |
| Disk usage | >80% | >95% |
| Swap usage | >50% of total | >80% of total |
| Load average (1m) | >2x CPU cores | >4x CPU cores |
| Network errors | >0/s | >10/s |
| Uptime | <1 day (recent reboot) | <1 hour |

## Example Output

```
Host Investigation: raspberrypi2
==================================

Role: worker | Address: 192.168.0.146
OS: Debian GNU/Linux 12 (bookworm) | Kernel: 6.1.0-rpi8-rpi-v8
Uptime: 14 days, 7 hours | Last reboot: 2026-01-25 03:15:00

Overall Status: ⚠️  DEGRADED

Resource Usage:
  CPU:    23% (4 cores)        ✅ OK
  Memory: 87% (3.4 GB / 3.9 GB)  ⚠️  WARNING (>80%)
  Disk:   72% (23 GB / 32 GB)    ✅ OK
  Swap:   1.2 GB / 2.0 GB        ⚠️  WARNING (60%)
  Load:   1.8 / 2.1 / 1.9        ✅ OK (4 cores)

Top Memory Consumers:
  1. immich-ml       — 2.1 GB (54%)
  2. immich-server   — 480 MB (12%)
  3. redis           — 210 MB (5%)
  4. postgres        — 180 MB (4%)

Services:
  Docker containers: 5 running, 0 stopped
  Systemd: 2 failed units ❌
    - telegraf.service (inactive since 2h ago)
    - promtail.service (failed, exit code 1)

Recent Errors (last hour):
  - [14:23] telegraf: connection refused to influxdb:8086
  - [14:23] telegraf: output write failed, retrying...
  - [14:45] promtail: error reading /var/log/syslog: permission denied

Past Learnings:
  - Learning #31: "telegraf fails when InfluxDB is unreachable"
    Solution: Check InfluxDB on primary host, restart telegraf after
    Success rate: 85% (6/7)

  - Learning #15: "promtail permission denied after log rotation"
    Solution: Restart promtail or fix logrotate config
    Success rate: 100% (3/3)

Recommended Actions:
  1. ⚠️  Memory: immich-ml using 54% of host RAM — consider increasing
     swap or setting container memory limit
  2. ❌ Fix telegraf: Check InfluxDB on raspberrypi (192.168.0.167),
     then restart: sudo systemctl restart telegraf
  3. ❌ Fix promtail: sudo systemctl restart promtail
     (or fix /var/log/syslog permissions)
  4. 📊 Monitor swap usage — if it keeps climbing, investigate
     memory leak in immich-ml
```

## Notes

- This skill is read-only during investigation (no restarts or changes)
- If remediation is needed, it will present options to the user
- All findings are logged to the investigation timeline
- Works for any host in config.yaml with SSH access
- Degrades gracefully: if Prometheus is down, SSH-only diagnostics still run
- If Docker is not installed on the host, container checks are skipped
