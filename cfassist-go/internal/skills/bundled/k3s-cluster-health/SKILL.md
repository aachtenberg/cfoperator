---
name: k3s-cluster-health
description: "Run a comprehensive health check of the entire K3s cluster including nodes, system pods, resource usage, and overall availability. Use this for daily health audits, before maintenance, after incidents, or when investigating cluster-wide issues. Keywords: cluster, health, nodes, k3s, kubernetes, k8s, audit, overview, status, capacity, resources."
---

# K3s Cluster Health

This skill performs a comprehensive health check of the entire K3s cluster, assessing node health, system components, workload status, and resource capacity.

## When to Use

- Daily/weekly cluster health audit
- Before performing maintenance
- After a cluster incident
- Investigating cluster-wide slowness
- Capacity planning
- When multiple services seem affected
- New cluster verification

## What It Does

The skill will:
1. Check all node health and status
2. Verify control plane components
3. Assess cluster resource capacity
4. Check all namespaces for unhealthy workloads
5. Review recent cluster events
6. Identify resource pressure (CPU, memory, disk)
7. Check system pods (kube-system, monitoring)
8. Summarize overall cluster health score
9. Provide recommendations for issues found

## Usage

Invoke: `/k3s-cluster-health`

No arguments required — checks the entire cluster.

## Implementation

### Phase 1: Node Health

Check all nodes in the cluster.

```
1. k8s_get_nodes()
   - List all nodes
   - Check Ready condition for each
   - Check for cordoned/unschedulable nodes
   - Note node roles (control-plane, worker)

2. For each node, check conditions:
   - Ready: Must be True
   - MemoryPressure: Should be False
   - DiskPressure: Should be False
   - PIDPressure: Should be False
   - NetworkUnavailable: Should be False

3. k8s_get_node_metrics()
   - CPU usage per node
   - Memory usage per node
   - Flag nodes over 80% utilized
```

### Phase 2: Control Plane Health

Verify K3s system components.

```
4. k8s_get_pods(namespace="kube-system")
   - Check all kube-system pods are Running
   - Key pods to verify:
     - coredns
     - local-path-provisioner
     - metrics-server
     - traefik (if using default ingress)
   - Note any CrashLoopBackOff or Pending pods

5. k8s_get_cluster_info()
   - Verify API server is reachable
   - Note cluster version
```

### Phase 3: Workload Health

Check all user workloads across namespaces.

```
6. k8s_get_namespaces()
   - List all namespaces
   - Note any in non-Active state

7. k8s_get_all_unhealthy()
   - Get all unhealthy pods cluster-wide
   - Get all unhealthy deployments
   - Group by namespace

8. For each namespace with issues:
   - Note which pods/deployments are affected
   - Categorize by issue type (Pending, CrashLoop, etc.)
```

### Phase 4: Resource Analysis

Assess cluster capacity.

```
9. Calculate cluster-wide metrics:
   - Total CPU capacity vs requests vs usage
   - Total memory capacity vs requests vs usage
   - Number of pods vs pod limit

10. k8s_get_pods(all_namespaces=True)
    - Count total pods
    - Count by phase (Running, Pending, Failed)
    - Note scheduling pressure

11. For each node:
    - Pods scheduled on node
    - Resources consumed vs allocatable
```

### Phase 5: Recent Events

Check for cluster-wide issues.

```
12. k8s_get_events(all_namespaces=True)
    - Get Warning events from last hour
    - Group by type (FailedScheduling, OOM, etc.)
    - Identify patterns

13. Look for common cluster issues:
    - Multiple FailedScheduling: Resource exhaustion
    - Multiple OOMKilled: Memory pressure
    - Multiple ImagePull errors: Registry issues
    - Multiple NodeNotReady: Network/node issues
```

### Phase 6: Service Mesh / Networking

Check cluster networking.

```
14. k8s_get_services(all_namespaces=True)
    - List all services
    - Note any without endpoints

15. Check ingress (if applicable):
    - k8s_get_pods(namespace="kube-system", labels="app.kubernetes.io/name=traefik")
    - Verify ingress controller is healthy
```

### Phase 7: Storage

Check persistent storage.

```
16. k8s_describe(resource_type="pvc", all_namespaces=True) or list PVCs
    - Check for Pending PVCs
    - Note any bound PVC issues

17. Check local-path-provisioner if used:
    - Verify it's running
    - Check for PV provisioning events
```

## Health Score Calculation

Calculate an overall health score (0-100):

| Component | Weight | Healthy Criteria |
|-----------|--------|------------------|
| Nodes | 30% | All Ready, no pressure conditions |
| Control Plane | 25% | All kube-system pods Running |
| Workloads | 25% | <5% pods unhealthy |
| Resources | 15% | <80% cluster utilization |
| Events | 5% | <10 Warning events/hour |

## Common Issues and Solutions

### Node NotReady
- Check kubelet logs on the node
- Verify network connectivity
- Check if node needs reboot

### kube-system Pods Failing
- CoreDNS crash: Check DNS config, resources
- metrics-server: May need more memory
- traefik: Check certificate/config issues

### High Resource Usage
- >80% CPU: Add nodes or reduce workloads
- >80% Memory: Add nodes, check for leaks
- High pod count: Check for runaway replicasets

### Many Pending Pods
- Resource exhaustion: Scale cluster
- Node affinity: Relax constraints
- PVC issues: Check storage provisioner

## Expected Output

The health check should produce:
1. **Node Summary**: X/Y nodes healthy, resource usage
2. **Control Plane**: All system pods status
3. **Workload Summary**: Healthy/unhealthy by namespace
4. **Resource Capacity**: CPU, memory, pod capacity
5. **Recent Issues**: Grouped warning events
6. **Health Score**: 0-100 with breakdown
7. **Recommendations**: Prioritized action items

Example summary:
```
## K3s Cluster Health Report

**Overall Score: 85/100**

### Nodes (5/5 healthy)
- k3s-01 (control-plane): Ready, CPU 45%, Mem 62%
- k3s-02 (worker): Ready, CPU 38%, Mem 55%
- k3s-03 (worker): Ready, CPU 52%, Mem 71%
- k3s-04 (worker): Ready, CPU 29%, Mem 48%
- k3s-05 (worker): Ready, CPU 61%, Mem 78%

### Control Plane: Healthy
- coredns: 2/2 Running
- metrics-server: 1/1 Running
- traefik: 1/1 Running

### Workloads: 2 issues
- apps/cfoperator-abc123: CrashLoopBackOff (1 restart)
- monitoring/prometheus-0: High memory (92%)

### Recommendations
1. Investigate cfoperator pod crash
2. Consider increasing prometheus memory limit
3. k3s-05 approaching memory pressure (78%)
```
