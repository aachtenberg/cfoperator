---
name: compare-hosts
description: "Compare metrics, configurations, and health across multiple hosts in the homelab. Use this to identify outliers, configuration drift, or fleet-wide patterns. Keywords: compare, hosts, fleet, drift, configuration, metrics, baseline."
---

# Compare Hosts

This skill compares multiple hosts across various dimensions to identify differences, drift, or anomalies in the fleet.

## When to Use

- One host behaving differently than others
- Investigating fleet-wide issues
- Verifying configuration consistency
- Finding outliers in metrics
- Post-deployment verification
- Capacity planning across hosts

## What It Does

The skill will:
1. Query metrics from all specified hosts
2. Compare resource usage (CPU, memory, disk)
3. Check container deployment consistency
4. Identify configuration drift
5. Compare investigation history and health trends
6. Highlight outliers and anomalies

## Usage

Invoke: `/compare-hosts [host1,host2,host3...]`

Examples:
- `/compare-hosts` - Compare all hosts
- `/compare-hosts raspberrypi,raspberrypi2` - Compare two specific hosts
- `/compare-hosts raspberry-pi2,raspberry-pi3,raspberry-pi4` - Compare secondary hosts

## Implementation

### Metrics to Compare

**Resource Usage:**
- CPU utilization (avg, max)
- Memory usage (available, cached, swap)
- Disk space (/, /var, /tmp)
- Network I/O

**Container Health:**
- Running containers count
- Container restart counts (24h)
- Unhealthy containers
- Container versions (detect version drift)

**System Health:**
- Uptime
- Load average (1m, 5m, 15m)
- Recent investigations count
- Investigation success rate
- Backup status

**Configuration:**
- Agent version
- Docker Compose differences
- Environment variable differences

## Tool Sequence

```
1. get_fleet_summary(hours=24)
2. For each host:
   - prometheus_query(node_cpu_seconds_total{instance="<host>"})
   - prometheus_query(node_memory_MemAvailable_bytes{instance="<host>"})
   - prometheus_query(node_filesystem_avail_bytes{instance="<host>"})
   - Query investigation count by outcome
3. Compare baselines for shared services
4. Identify drift using drift_events table
5. Generate comparison matrix

Output format:
- Tabular comparison
- Outlier highlights
- Drift warnings
- Recommendations
```

## Example Output

```
Host Comparison: All Hosts (4 total)
=====================================

Resource Usage:
Host            CPU %   Memory %   Disk %   Load (1m)   Uptime
raspberry-pi    12.3    45.2       67.1     0.85        14d 3h
raspberry-pi2   45.7    82.3       71.2     2.34        14d 2h  ⚠️ HIGH CPU
raspberry-pi3   14.1    48.9       68.5     0.92        14d 3h
raspberry-pi4   11.8    43.1       65.4     0.78        14d 3h

Container Health:
Host            Running   Restarts (24h)   Unhealthy   Version
raspberry-pi    12        0                0           v1.17.0
raspberry-pi2   8         2                1           v1.17.0  ⚠️ 1 unhealthy
raspberry-pi3   8         0                0           v1.17.0
raspberry-pi4   8         0                0           v1.16.0  ⚠️ VERSION DRIFT

Investigations (24h):
Host            Total   Resolved   Failed   Success Rate
raspberry-pi    0       0          0        -
raspberry-pi2   3       2          1        66%
raspberry-pi3   1       1          0        100%
raspberry-pi4   0       0          0        -

Key Findings:
1. ⚠️ raspberry-pi2: High CPU (45.7%) and 1 unhealthy container
2. ⚠️ raspberry-pi4: Running older version (v1.16.0)
3. ✓ raspberry-pi, pi3, pi4: Resource usage normal
4. Configuration drift detected: pi4 needs update

Recommendations:
1. Investigate high CPU on raspberry-pi2 (likely container issue)
2. Deploy v1.17.0 to raspberry-pi4
3. Check unhealthy container on raspberry-pi2: `docker ps -a`
```

## Advanced Queries

The skill can also:
- Compare specific time ranges: `/compare-hosts --hours=168` (7 days)
- Focus on specific metrics: `/compare-hosts --metrics=cpu,memory`
- Show only outliers: `/compare-hosts --outliers-only`

## Notes

- Uses Prometheus `instance` label (raspberry-pi, raspberry-pi2, etc.)
- Requires fleet_summary tool and Prometheus access
- Highlights differences >20% as potential outliers
- Cross-references with drift_events table for known configuration changes
