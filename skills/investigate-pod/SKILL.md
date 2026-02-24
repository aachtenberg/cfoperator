---
name: investigate-pod
description: "Run a systematic investigation of a Kubernetes pod's health, logs, events, and resource usage. Use this when a pod is crashing, stuck in pending, not starting, OOMKilled, or behaving unexpectedly. Keywords: pod, container, k8s, kubernetes, k3s, crash, restart, pending, error, logs, OOM, crashloop."
---

# Investigate Pod

This skill performs a comprehensive investigation of a Kubernetes pod, gathering diagnostics from kubectl to identify why a pod is unhealthy or misbehaving.

## When to Use

- Pod is in CrashLoopBackOff
- Pod stuck in Pending state
- Pod keeps restarting
- Pod is OOMKilled
- Pod shows ImagePullBackOff
- Container exits with non-zero code
- Application not responding but pod shows Running
- Investigating why a deployment isn't rolling out

## What It Does

The skill will:
1. Get pod status and phase
2. Check pod events for scheduling/startup issues
3. Get container statuses and restart counts
4. Retrieve logs from current and previous container instances
5. Check resource requests/limits vs node capacity
6. Describe pod for full configuration details
7. Check related deployment status
8. Search past learnings for similar issues
9. Provide summary with root cause and recommendations

## Usage

Invoke: `/investigate-pod <namespace> <pod-name>`

Examples:
- `/investigate-pod apps cfoperator-abc123` - Investigate cfoperator pod
- `/investigate-pod monitoring prometheus-0` - Investigate prometheus pod
- `/investigate-pod default nginx-xyz789` - Investigate nginx pod

## Implementation

### Phase 1: Pod Status Check

Get current state of the pod.

```
1. k8s_get_pod_status(namespace, pod_name)
   - Phase (Running, Pending, Failed, Succeeded, Unknown)
   - Pod conditions (Ready, ContainersReady, Initialized, PodScheduled)
   - Container statuses (state, restartCount, ready)
   - Pod IP and host IP

2. If phase != Running or restartCount > 0:
   - Flag issue immediately
   - Note last termination reason if available
```

### Phase 2: Events Analysis

Check events for scheduling, image pull, or startup failures.

```
3. k8s_get_events(namespace, resource_name=pod_name)
   - Look for Warning events
   - Common issues:
     - FailedScheduling (insufficient resources, node selector)
     - FailedMount (PVC issues, secrets not found)
     - BackOff (container crashloop)
     - Unhealthy (liveness/readiness probe failures)
     - ImagePullBackOff (wrong image, auth failure)

4. If scheduling issues found:
   - Check node resources with k8s_get_node_metrics()
   - Check node taints/labels if node selector used
```

### Phase 3: Log Analysis

Get container logs to find application-level errors.

```
5. k8s_get_pod_logs(namespace, pod_name, lines=100)
   - Current container logs
   - Look for ERROR, FATAL, panic, exception patterns

6. If restartCount > 0:
   k8s_get_pod_logs(namespace, pod_name, previous=True, lines=100)
   - Previous container logs (before crash)
   - Often reveals OOM or startup failures
```

### Phase 4: Configuration Review

Check pod configuration for common issues.

```
7. k8s_describe(resource_type="pod", name=pod_name, namespace=namespace)
   - Resource requests and limits
   - Environment variables (check for missing config)
   - Volume mounts
   - Node assignment
   - QoS class

8. If OOMKilled:
   - Compare memory limit to actual usage
   - Recommend increasing limit or fixing memory leak
```

### Phase 5: Related Resources

Check parent deployment and siblings.

```
9. If pod is owned by a deployment/replicaset:
   - Extract deployment name from pod name (strip random suffix)
   - k8s_get_deployments(namespace)
   - k8s_rollout_status(namespace, deployment)
   - Check if all replicas having same issue

10. k8s_get_pods(namespace, labels=<app-label>)
    - Check if sibling pods are healthy
    - If only one pod failing, might be node-specific
```

### Phase 6: Knowledge Base

```
11. query_learnings(query="pod crash <error-type>", services=[<app-name>])
    - Find past solutions for similar issues
    - Check if this is a known pattern
```

## Common Issues and Solutions

### CrashLoopBackOff
- Check logs for application startup errors
- Verify environment variables and config
- Check if dependencies are available
- Review resource limits (OOMKilled?)

### Pending
- FailedScheduling: Check node resources, taints, affinity
- Missing PVC: Check if PersistentVolumeClaim exists
- Image pull: Check image name, registry auth

### OOMKilled
- Increase memory limit
- Check for memory leaks
- Profile application memory usage

### ImagePullBackOff
- Verify image name and tag exist
- Check registry credentials
- Try pulling image manually on node

## Expected Output

The investigation should produce:
1. **Status Summary**: Pod phase, container states, restart count
2. **Events**: Recent warnings and their meanings
3. **Logs**: Key error messages from container logs
4. **Root Cause**: Best guess at why pod is unhealthy
5. **Recommendations**: Specific actions to fix the issue
6. **Related Learning**: Any past solutions that apply
