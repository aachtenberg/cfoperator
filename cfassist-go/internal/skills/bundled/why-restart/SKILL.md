---
name: why-restart
description: "Investigate why a container restarted by analyzing exit codes, logs before crash, OOM events, and restart patterns. Use when containers are restarting unexpectedly. Keywords: restart, crash, OOM, exit code, container died, crashed."
---

# Why Restart?

This skill performs root cause analysis on container restarts, combining exit codes, pre-crash logs, system events, and historical patterns to determine why a container restarted.

## When to Use

- Container restarted unexpectedly
- Investigating restart loops
- High restart count in metrics
- OOM (Out of Memory) alerts
- Understanding crash patterns

## What It Does

The skill will:
1. Get container restart history and exit codes
2. Analyze logs BEFORE the restart (critical window)
3. Check for OOM (Out of Memory) kills
4. Query system metrics at time of restart
5. Search for similar restart patterns in past investigations
6. Identify restart triggers (crash, OOM, deployment, manual)
7. Provide root cause and prevention steps

## Usage

Invoke: `/why-restart <container_name> [--count=N]`

Examples:
- `/why-restart sre-agent` - Investigate most recent restart
- `/why-restart prometheus --count=5` - Analyze last 5 restarts
- `/why-restart immich-ml` - Check why ML container keeps restarting

## Implementation

### Detection Strategy

**Exit Code Analysis:**
- 0 = Clean shutdown (expected)
- 1 = Application error
- 137 = SIGKILL (likely OOM)
- 139 = SIGSEGV (segmentation fault)
- 143 = SIGTERM (manual stop or Docker stop)

**OOM Detection:**
- Check `docker inspect` for OOMKilled flag
- Query kernel logs via `dmesg` for oom-killer
- Check node_exporter OOM metrics
- Compare memory limit vs actual usage

**Log Analysis Window:**
- Last 100 lines before restart timestamp
- Look for: ERROR, FATAL, panic, exception, segfault
- Pattern matching against known crash signatures

## Tool Sequence

```
1. docker_inspect(container_name)
   - Get restart count, last exit code, OOMKilled flag
   - Extract last restart timestamp

2. Get logs from BEFORE restart:
   shell("docker logs --since <2h> --until <restart_time> <container> --tail 100")

3. Check system metrics at restart time:
   prometheus_query(container_memory_usage{container="<name>"}[5m] @ <timestamp>)
   prometheus_query(node_oom_kills_total @ <timestamp>)

4. Search investigation history:
   Query investigations where:
   - trigger contains container_name
   - outcome = resolved
   - findings mention "restart" or "OOM"

5. Search learnings:
   get_relevant_learnings(
     query="container restart crash OOM",
     services=[container_name]
   )

6. Generate root cause analysis:
   - Primary cause (exit code + logs + OOM flag)
   - Contributing factors (memory pressure, dependencies)
   - Timeline reconstruction
   - Prevention recommendations
```

## Example Output

```
Restart Analysis: immich-ml
===========================

Restart Event: 2026-02-05 18:42:13 UTC (14 minutes ago)
Exit Code: 137 (SIGKILL - likely OOM)
OOMKilled: true ✓

Root Cause: Out of Memory Kill
-------------------------------
The container was killed by the Linux OOM killer after exceeding its memory limit.

Evidence:
1. Exit code 137 (SIGKILL)
2. OOMKilled flag set in container state
3. Memory usage at crash: 3.95 GB / 4 GB limit (98.7%)
4. Kernel logs show: "oom-killer: Kill process immich-ml"

Timeline:
18:40:00 - Memory: 2.1 GB (52%)
18:41:00 - Memory: 3.2 GB (80%)
18:41:45 - Memory: 3.8 GB (95%)
18:42:10 - Memory: 3.95 GB (98%) - peak
18:42:13 - OOM kill triggered

Pre-Crash Logs (last 10 lines):
[18:41:58] INFO: Processing image batch 45/50
[18:42:05] INFO: Loading model embeddings...
[18:42:08] WARNING: Memory allocation size: 1.2 GB
[18:42:11] ERROR: numpy.core._exceptions.MemoryError
[18:42:12] <container killed>

Past Learnings:
- Learning #28: "immich-ml OOM during batch processing"
  Solution: Increase memory limit to 6 GB or reduce batch size
  Success rate: 100% (5/5)

Recommendations:
1. Increase memory limit from 4 GB to 6 GB
   Edit: docker-compose.yml
   deploy:
     resources:
       limits:
         memory: 6G

2. Alternative: Reduce batch size in immich config
   MACHINE_LEARNING_BATCH_SIZE: 8 (currently 16)

3. Monitor memory trends after change

Prevention:
- Add memory alert at 80% threshold
- Consider model optimization
```

## Advanced Usage

**Compare restart patterns:**
```
/why-restart sre-agent --pattern
```
Shows restart frequency, times of day, correlation with other events.

**Restart loop detection:**
```
/why-restart <container> --loop
```
Detects restart loops (>3 restarts in 5 minutes) and suggests cooldown.

## Notes

- Works best with recent restarts (logs still available)
- Requires Docker socket access and Prometheus metrics
- For old restarts, relies on investigation history
- Integrates with learning extraction
- Can trigger auto-remediation if learning has high success rate
