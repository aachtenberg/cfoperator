---
name: investigate-deployment
description: "Run a systematic investigation of a Kubernetes deployment's health, rollout status, replica health, and related resources. Use this when a deployment won't scale, is stuck rolling out, has unhealthy pods, or isn't serving traffic. Keywords: deployment, rollout, replica, scale, k8s, kubernetes, k3s, service, ingress, availability."
---

# Investigate Deployment

This skill performs a comprehensive investigation of a Kubernetes deployment, checking rollout status, replica health, service connectivity, and configuration.

## When to Use

- Deployment stuck in rollout
- Pods not becoming ready
- Service not receiving traffic
- Scaling issues (can't scale up/down)
- After a failed deployment update
- Investigating availability issues
- Capacity planning for deployments

## What It Does

The skill will:
1. Get deployment status (replicas, ready, available)
2. Check rollout status and history
3. Investigate individual pod health
4. Verify service is selecting pods correctly
5. Check resource quotas and limits
6. Review recent events
7. Compare current vs desired state
8. Search past learnings for similar issues
9. Provide summary with findings and recommendations

## Usage

Invoke: `/investigate-deployment <namespace> <deployment-name>`

Examples:
- `/investigate-deployment apps cfoperator` - Investigate cfoperator deployment
- `/investigate-deployment ai llm-gateway` - Investigate llm-gateway deployment
- `/investigate-deployment monitoring alertmanager` - Investigate alertmanager deployment

## Implementation

### Phase 1: Deployment Status

Get high-level deployment health.

```
1. k8s_get_deployments(namespace)
   - Filter to target deployment
   - Check: replicas, ready, available, updated
   - Flag if ready < desired or available < desired

2. k8s_rollout_status(namespace, deployment)
   - Is rollout complete?
   - If stuck, how long?
```

### Phase 2: Pod Health

Check all pods managed by this deployment.

```
3. k8s_get_pods(namespace, labels="app=<deployment-name>")
   - List all pods for this deployment
   - Check phase of each pod
   - Note any non-Running pods

4. For each unhealthy pod:
   - k8s_get_pod_status(namespace, pod_name)
   - Note restart counts, termination reasons
   - This may trigger /investigate-pod skill
```

### Phase 3: Events

Check deployment and pod events.

```
5. k8s_get_events(namespace, resource_name=deployment)
   - Deployment-level events
   - ScalingReplicaSet events
   - ProgressDeadlineExceeded

6. k8s_get_events(namespace)
   - Look for Warning events related to deployment pods
   - Filter by pod name prefix matching deployment
```

### Phase 4: Service Connectivity

Verify traffic can reach pods.

```
7. k8s_get_services(namespace)
   - Find service(s) for this deployment
   - Match by selector labels
   - Note service type, ports, cluster IP

8. k8s_describe(resource_type="service", name=service_name, namespace=namespace)
   - Check endpoints
   - If no endpoints: selector doesn't match pod labels
   - If endpoints exist: pods are receiving traffic

9. k8s_describe(resource_type="endpoints", name=service_name, namespace=namespace)
   - Verify pods are registered as endpoints
   - If empty, service selector is wrong
```

### Phase 5: Configuration Review

Check deployment spec for issues.

```
10. k8s_describe(resource_type="deployment", name=deployment, namespace=namespace)
    - Strategy (RollingUpdate vs Recreate)
    - MaxUnavailable, MaxSurge settings
    - Resource requests/limits
    - Node selector, affinity
    - Update timestamp

11. If stuck rolling:
    - Check maxUnavailable isn't 0 with only 1 replica
    - Check pod disruption budgets
    - Verify new pods can schedule
```

### Phase 6: Resource Analysis

Check if cluster has capacity.

```
12. k8s_get_node_metrics()
    - Overall cluster resource usage
    - Flag if nodes are near capacity

13. If pods pending due to resources:
    - Compare requested resources to available
    - Recommend scaling cluster or reducing requests
```

### Phase 7: Knowledge Base

```
14. query_learnings(query="deployment rollout <issue>", services=[deployment])
    - Find past solutions
    - Check for known patterns
```

## Common Issues and Solutions

### Rollout Stuck
- **MaxUnavailable=0 with 1 replica**: Can't take down the only pod
  - Solution: Set maxUnavailable=1 or increase replicas
- **PodDisruptionBudget blocking**: PDB too restrictive
  - Solution: Adjust PDB or force rollout
- **New pods failing**: Underlying pod issue
  - Solution: Investigate pods with /investigate-pod

### Pods Not Ready
- **Readiness probe failing**: App not responding on probeendpoint
  - Solution: Check probe config, increase initialDelaySeconds
- **Slow startup**: App takes longer than timeout
  - Solution: Increase timeoutSeconds, periodSeconds

### Service No Endpoints
- **Selector mismatch**: Service selector doesn't match pod labels
  - Solution: Fix service selector or pod labels
- **All pods unhealthy**: Pods exist but not ready
  - Solution: Fix pod issues first

### Can't Scale Up
- **Insufficient resources**: Cluster at capacity
  - Solution: Add nodes or reduce resource requests
- **Node affinity**: No nodes match requirements
  - Solution: Relax affinity rules or add matching nodes

## Expected Output

The investigation should produce:
1. **Deployment Status**: Replica counts, rollout state
2. **Pod Summary**: Health of all managed pods
3. **Service Status**: Whether traffic routing works
4. **Events**: Recent warnings and their meanings
5. **Root Cause**: Best guess at the issue
6. **Recommendations**: Specific actions to resolve
7. **Related Learning**: Past solutions that apply
