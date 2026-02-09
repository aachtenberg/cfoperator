---
name: investigate-container
description: "Run a systematic investigation of a Docker container's health, logs, and metrics. Use this when a container is misbehaving, restarting, or showing errors. Keywords: container, docker, investigate, troubleshoot, debug, logs, health."
---

# Investigate Container

This skill performs a comprehensive investigation of a Docker container, gathering diagnostics from multiple sources to identify issues.

## When to Use

- Container is restarting frequently
- Container shows unhealthy status
- User reports issues with a specific service
- Investigating alerts about a container
- Need to understand container behavior

## What It Does

The skill will:
1. Check container status and health
2. Analyze recent logs for errors
3. Query Prometheus metrics (CPU, memory, restart count)
4. Check for recent configuration changes
5. Review past investigations for this container
6. Search for relevant learnings from similar issues
7. Provide a summary and recommendations

## Usage

Simply invoke: `/investigate-container <container_name>`

Examples:
- `/investigate-container sre-agent`
- `/investigate-container sre-dashboard`
- `/investigate-container prometheus`

## Implementation

The skill uses CFOperator's investigation tools in a structured workflow:

1. **System State**: Get current container status
2. **Logs Analysis**: Pull last 200 lines, identify errors
3. **Metrics Check**: Query Prometheus for resource usage and trends
4. **Change Detection**: Check what_changed to see recent modifications
5. **Past Context**: Search investigation history for this container
6. **Learning Search**: Query get_relevant_learnings for known patterns
7. **Summary**: Synthesize findings into actionable insights

## Tool Sequence

```
1. docker_inspect(container_name)
2. docker_logs(container_name, lines=200)
3. prometheus_query(container_cpu_usage{container="<name>"})
4. prometheus_query(container_memory_usage{container="<name>"})
5. prometheus_query(container_restart_count{container="<name>"})
6. what_changed(services=[container_name], hours=24)
7. get_relevant_learnings(query="container issues", services=[container_name])
8. Generate summary with:
   - Current state
   - Key findings
   - Possible root causes
   - Recommended actions
```

## Example Output

```
Container Investigation: sre-agent
=====================================

Status: Running (unhealthy)
Uptime: 2h 14m
Restarts (24h): 3

Key Findings:
- Memory usage climbing steadily (currently 87%)
- PostgreSQL connection errors in logs (last 30 min)
- Database was restarted 2h ago (what_changed)

Past Learnings:
- Learning #42: "sre-agent requires DB restart to reconnect"
  Solution: Restart agent after DB changes
  Success rate: 90% (9/10)

Recommended Actions:
1. Restart sre-agent container
2. Verify PostgreSQL is healthy
3. Monitor memory usage after restart
```

## Notes

- This skill is read-only during investigation
- If remediation is needed, it will present options to the user
- All findings are logged to the investigation timeline
- Leverages the new learning extraction system (v1.17.0+)
