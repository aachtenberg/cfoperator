"""
Kubernetes Tools for K3s Cluster Management
============================================

Provides tools for CFOperator to interact with the k3s cluster
for troubleshooting, log retrieval, service management, etc.
"""

import subprocess
import json
import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger("cfoperator.tools.k8s")


class K8sTools:
    """
    Kubernetes-based tools for cluster operations.

    CFOperator uses kubectl to:
    - Get pod status and logs
    - Describe resources
    - Check deployments and services
    - Execute commands in pods
    - Monitor cluster health
    """

    def __init__(self, kubeconfig: Optional[str] = None, context: Optional[str] = None):
        """
        Initialize K8s tools.

        Args:
            kubeconfig: Optional path to kubeconfig file
            context: Optional kubectl context to use
        """
        self.kubeconfig = kubeconfig
        self.context = context
        logger.info("K8s tools initialized")

    def _kubectl_cmd(self, args: List[str]) -> List[str]:
        """Build kubectl command with optional config."""
        cmd = ['kubectl']
        if self.kubeconfig:
            cmd.extend(['--kubeconfig', self.kubeconfig])
        if self.context:
            cmd.extend(['--context', self.context])
        cmd.extend(args)
        return cmd

    def _run_kubectl(self, args: List[str], timeout: int = 30) -> Dict[str, Any]:
        """Execute kubectl command and return result."""
        cmd = self._kubectl_cmd(args)
        try:
            logger.debug(f"Running: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            return {
                'success': result.returncode == 0,
                'stdout': result.stdout,
                'stderr': result.stderr,
                'exit_code': result.returncode
            }
        except subprocess.TimeoutExpired:
            return {
                'success': False,
                'error': f'Command timed out after {timeout}s'
            }
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }

    # =========================================================================
    # Pod Operations
    # =========================================================================

    def get_pods(self, namespace: str = "default", labels: Optional[str] = None,
                 all_namespaces: bool = False) -> Dict[str, Any]:
        """
        List pods in a namespace.

        Args:
            namespace: Kubernetes namespace
            labels: Optional label selector (e.g., "app=nginx")
            all_namespaces: If True, list pods in all namespaces

        Returns:
            Dict with pod list or error
        """
        args = ['get', 'pods', '-o', 'json']
        if all_namespaces:
            args.append('-A')
        else:
            args.extend(['-n', namespace])
        if labels:
            args.extend(['-l', labels])

        result = self._run_kubectl(args)
        if result['success']:
            try:
                pods = json.loads(result['stdout'])
                return {
                    'success': True,
                    'pods': pods.get('items', []),
                    'count': len(pods.get('items', []))
                }
            except json.JSONDecodeError:
                return {
                    'success': True,
                    'raw': result['stdout']
                }
        return result

    def get_pod_status(self, namespace: str, pod_name: str) -> Dict[str, Any]:
        """
        Get detailed status of a specific pod.

        Args:
            namespace: Kubernetes namespace
            pod_name: Name of the pod

        Returns:
            Dict with pod status details
        """
        args = ['get', 'pod', pod_name, '-n', namespace, '-o', 'json']
        result = self._run_kubectl(args)
        if result['success']:
            try:
                pod = json.loads(result['stdout'])
                status = pod.get('status', {})
                return {
                    'success': True,
                    'name': pod_name,
                    'namespace': namespace,
                    'phase': status.get('phase'),
                    'conditions': status.get('conditions', []),
                    'containerStatuses': status.get('containerStatuses', []),
                    'startTime': status.get('startTime'),
                    'hostIP': status.get('hostIP'),
                    'podIP': status.get('podIP')
                }
            except json.JSONDecodeError:
                return {'success': False, 'error': 'Failed to parse pod JSON'}
        return result

    def get_pod_logs(self, namespace: str, pod_name: str,
                     container: Optional[str] = None,
                     lines: int = 100,
                     previous: bool = False,
                     since: Optional[str] = None) -> Dict[str, Any]:
        """
        Get logs from a pod.

        Args:
            namespace: Kubernetes namespace
            pod_name: Name of the pod
            container: Optional container name (required if multiple containers)
            lines: Number of lines to return (tail)
            previous: Get logs from previous container instance
            since: Only return logs newer than duration (e.g., "1h", "5m")

        Returns:
            Dict with log output
        """
        args = ['logs', pod_name, '-n', namespace, f'--tail={lines}']
        if container:
            args.extend(['-c', container])
        if previous:
            args.append('--previous')
        if since:
            args.append(f'--since={since}')

        result = self._run_kubectl(args, timeout=60)
        if result['success']:
            return {
                'success': True,
                'logs': result['stdout'],
                'pod': pod_name,
                'namespace': namespace,
                'container': container,
                'lines': lines
            }
        return result

    # =========================================================================
    # Deployment Operations
    # =========================================================================

    def get_deployments(self, namespace: str = "default",
                        all_namespaces: bool = False) -> Dict[str, Any]:
        """
        List deployments.

        Args:
            namespace: Kubernetes namespace
            all_namespaces: If True, list in all namespaces

        Returns:
            Dict with deployment list
        """
        args = ['get', 'deployments', '-o', 'json']
        if all_namespaces:
            args.append('-A')
        else:
            args.extend(['-n', namespace])

        result = self._run_kubectl(args)
        if result['success']:
            try:
                deps = json.loads(result['stdout'])
                items = deps.get('items', [])
                deployments = []
                for d in items:
                    meta = d.get('metadata', {})
                    spec = d.get('spec', {})
                    status = d.get('status', {})
                    deployments.append({
                        'name': meta.get('name'),
                        'namespace': meta.get('namespace'),
                        'replicas': spec.get('replicas', 0),
                        'ready': status.get('readyReplicas', 0),
                        'available': status.get('availableReplicas', 0),
                        'updated': status.get('updatedReplicas', 0)
                    })
                return {
                    'success': True,
                    'deployments': deployments,
                    'count': len(deployments)
                }
            except json.JSONDecodeError:
                return {'success': True, 'raw': result['stdout']}
        return result

    def rollout_status(self, namespace: str, deployment: str) -> Dict[str, Any]:
        """
        Get rollout status of a deployment.

        Args:
            namespace: Kubernetes namespace
            deployment: Deployment name

        Returns:
            Dict with rollout status
        """
        args = ['rollout', 'status', f'deployment/{deployment}', '-n', namespace]
        result = self._run_kubectl(args, timeout=10)
        return {
            'success': result['success'],
            'status': result.get('stdout', '').strip(),
            'deployment': deployment,
            'namespace': namespace
        }

    def rollout_restart(self, namespace: str, deployment: str) -> Dict[str, Any]:
        """
        Restart a deployment by triggering a rolling update.

        Args:
            namespace: Kubernetes namespace
            deployment: Deployment name

        Returns:
            Dict with restart result
        """
        args = ['rollout', 'restart', f'deployment/{deployment}', '-n', namespace]
        result = self._run_kubectl(args)
        return {
            'success': result['success'],
            'message': result.get('stdout', '').strip() or result.get('stderr', '').strip(),
            'deployment': deployment,
            'namespace': namespace
        }

    # =========================================================================
    # Service Operations
    # =========================================================================

    def get_services(self, namespace: str = "default",
                     all_namespaces: bool = False) -> Dict[str, Any]:
        """
        List services.

        Args:
            namespace: Kubernetes namespace
            all_namespaces: If True, list in all namespaces

        Returns:
            Dict with service list
        """
        args = ['get', 'services', '-o', 'json']
        if all_namespaces:
            args.append('-A')
        else:
            args.extend(['-n', namespace])

        result = self._run_kubectl(args)
        if result['success']:
            try:
                svcs = json.loads(result['stdout'])
                items = svcs.get('items', [])
                services = []
                for s in items:
                    meta = s.get('metadata', {})
                    spec = s.get('spec', {})
                    services.append({
                        'name': meta.get('name'),
                        'namespace': meta.get('namespace'),
                        'type': spec.get('type'),
                        'clusterIP': spec.get('clusterIP'),
                        'ports': spec.get('ports', []),
                        'selector': spec.get('selector', {})
                    })
                return {
                    'success': True,
                    'services': services,
                    'count': len(services)
                }
            except json.JSONDecodeError:
                return {'success': True, 'raw': result['stdout']}
        return result

    # =========================================================================
    # Events and Describe
    # =========================================================================

    def get_events(self, namespace: str = "default",
                   resource_name: Optional[str] = None,
                   all_namespaces: bool = False) -> Dict[str, Any]:
        """
        Get events from the cluster.

        Args:
            namespace: Kubernetes namespace
            resource_name: Optional filter by involved object name
            all_namespaces: If True, get events from all namespaces

        Returns:
            Dict with events
        """
        args = ['get', 'events', '-o', 'json', '--sort-by=.lastTimestamp']
        if all_namespaces:
            args.append('-A')
        else:
            args.extend(['-n', namespace])
        if resource_name:
            args.extend(['--field-selector', f'involvedObject.name={resource_name}'])

        result = self._run_kubectl(args)
        if result['success']:
            try:
                events = json.loads(result['stdout'])
                items = events.get('items', [])
                # Return last 50 events
                formatted = []
                for e in items[-50:]:
                    formatted.append({
                        'type': e.get('type'),
                        'reason': e.get('reason'),
                        'message': e.get('message'),
                        'object': e.get('involvedObject', {}).get('name'),
                        'kind': e.get('involvedObject', {}).get('kind'),
                        'count': e.get('count', 1),
                        'lastTimestamp': e.get('lastTimestamp')
                    })
                return {
                    'success': True,
                    'events': formatted,
                    'count': len(formatted)
                }
            except json.JSONDecodeError:
                return {'success': True, 'raw': result['stdout']}
        return result

    def describe(self, resource_type: str, name: str,
                 namespace: str = "default") -> Dict[str, Any]:
        """
        Describe a Kubernetes resource.

        Args:
            resource_type: Type of resource (pod, deployment, service, etc.)
            name: Name of the resource
            namespace: Kubernetes namespace

        Returns:
            Dict with describe output
        """
        args = ['describe', resource_type, name, '-n', namespace]
        result = self._run_kubectl(args, timeout=30)
        return {
            'success': result['success'],
            'description': result.get('stdout', ''),
            'resource_type': resource_type,
            'name': name,
            'namespace': namespace
        }

    # =========================================================================
    # Node Operations
    # =========================================================================

    def get_nodes(self) -> Dict[str, Any]:
        """
        List all nodes in the cluster.

        Returns:
            Dict with node list and status
        """
        args = ['get', 'nodes', '-o', 'json']
        result = self._run_kubectl(args)
        if result['success']:
            try:
                nodes_data = json.loads(result['stdout'])
                items = nodes_data.get('items', [])
                nodes = []
                for n in items:
                    meta = n.get('metadata', {})
                    status = n.get('status', {})
                    conditions = {c['type']: c['status'] for c in status.get('conditions', [])}
                    nodes.append({
                        'name': meta.get('name'),
                        'labels': meta.get('labels', {}),
                        'ready': conditions.get('Ready', 'Unknown'),
                        'memoryPressure': conditions.get('MemoryPressure', 'Unknown'),
                        'diskPressure': conditions.get('DiskPressure', 'Unknown'),
                        'kubeletVersion': status.get('nodeInfo', {}).get('kubeletVersion'),
                        'osImage': status.get('nodeInfo', {}).get('osImage'),
                        'architecture': status.get('nodeInfo', {}).get('architecture')
                    })
                return {
                    'success': True,
                    'nodes': nodes,
                    'count': len(nodes)
                }
            except json.JSONDecodeError:
                return {'success': True, 'raw': result['stdout']}
        return result

    def get_node_metrics(self) -> Dict[str, Any]:
        """
        Get resource usage metrics for nodes (requires metrics-server).

        Returns:
            Dict with node metrics
        """
        args = ['top', 'nodes', '--no-headers']
        result = self._run_kubectl(args, timeout=15)
        if result['success']:
            lines = result['stdout'].strip().split('\n')
            metrics = []
            for line in lines:
                if line.strip():
                    parts = line.split()
                    if len(parts) >= 5:
                        metrics.append({
                            'name': parts[0],
                            'cpu': parts[1],
                            'cpu_percent': parts[2],
                            'memory': parts[3],
                            'memory_percent': parts[4]
                        })
            return {
                'success': True,
                'metrics': metrics
            }
        return result

    # =========================================================================
    # Pod Exec
    # =========================================================================

    def exec_pod(self, namespace: str, pod_name: str, command: str,
                 container: Optional[str] = None,
                 timeout: int = 30) -> Dict[str, Any]:
        """
        Execute a command inside a pod.

        Args:
            namespace: Kubernetes namespace
            pod_name: Name of the pod
            command: Command to execute
            container: Optional container name
            timeout: Command timeout in seconds

        Returns:
            Dict with command output
        """
        args = ['exec', pod_name, '-n', namespace]
        if container:
            args.extend(['-c', container])
        args.extend(['--', 'sh', '-c', command])

        result = self._run_kubectl(args, timeout=timeout)
        return {
            'success': result['success'],
            'stdout': result.get('stdout', ''),
            'stderr': result.get('stderr', ''),
            'pod': pod_name,
            'namespace': namespace,
            'command': command
        }

    # =========================================================================
    # Cluster Overview
    # =========================================================================

    def get_cluster_info(self) -> Dict[str, Any]:
        """
        Get cluster information.

        Returns:
            Dict with cluster info
        """
        args = ['cluster-info']
        result = self._run_kubectl(args, timeout=10)
        return {
            'success': result['success'],
            'info': result.get('stdout', '').strip()
        }

    def get_namespaces(self) -> Dict[str, Any]:
        """
        List all namespaces.

        Returns:
            Dict with namespace list
        """
        args = ['get', 'namespaces', '-o', 'json']
        result = self._run_kubectl(args)
        if result['success']:
            try:
                ns_data = json.loads(result['stdout'])
                namespaces = [
                    {
                        'name': n.get('metadata', {}).get('name'),
                        'status': n.get('status', {}).get('phase')
                    }
                    for n in ns_data.get('items', [])
                ]
                return {
                    'success': True,
                    'namespaces': namespaces,
                    'count': len(namespaces)
                }
            except json.JSONDecodeError:
                return {'success': True, 'raw': result['stdout']}
        return result

    def get_all_unhealthy(self) -> Dict[str, Any]:
        """
        Get all unhealthy resources across the cluster.

        Returns:
            Dict with unhealthy pods and deployments
        """
        unhealthy = {
            'pods': [],
            'deployments': [],
            'restarted_pods': []
        }

        # Check pods
        pods_result = self.get_pods(all_namespaces=True)
        if pods_result['success']:
            for pod in pods_result.get('pods', []):
                metadata = pod.get('metadata', {})
                status = pod.get('status', {})
                phase = pod.get('status', {}).get('phase')
                namespace = metadata.get('namespace')
                name = metadata.get('name')
                container_statuses = status.get('containerStatuses', [])

                if phase not in ['Running', 'Succeeded']:
                    unhealthy['pods'].append({
                        'name': name,
                        'namespace': namespace,
                        'phase': phase
                    })

                restart_count = sum(cs.get('restartCount', 0) for cs in container_statuses)
                waiting_reasons = [
                    cs.get('state', {}).get('waiting', {}).get('reason')
                    for cs in container_statuses
                    if cs.get('state', {}).get('waiting', {}).get('reason')
                ]
                last_terminated = []
                recent_restart = False
                for cs in container_statuses:
                    terminated = cs.get('lastState', {}).get('terminated')
                    if terminated:
                        finished_at = terminated.get('finishedAt', '')
                        last_terminated.append({
                            'container': cs.get('name'),
                            'exit_code': terminated.get('exitCode'),
                            'reason': terminated.get('reason'),
                            'finished_at': finished_at
                        })
                        # Check if the restart was recent (within last 2 hours)
                        if finished_at:
                            try:
                                from datetime import datetime, timezone, timedelta
                                # Parse k8s timestamp (e.g., "2026-03-20T22:35:55Z")
                                ts = datetime.fromisoformat(finished_at.replace('Z', '+00:00'))
                                if datetime.now(timezone.utc) - ts < timedelta(hours=2):
                                    recent_restart = True
                            except (ValueError, TypeError):
                                recent_restart = True  # Can't parse — assume recent

                # Only report pods with recent restarts, active waiting states,
                # or currently not running. Stale restart counts are noise.
                if waiting_reasons or (restart_count > 0 and recent_restart):
                    unhealthy['restarted_pods'].append({
                        'name': name,
                        'namespace': namespace,
                        'phase': phase,
                        'restart_count': restart_count,
                        'waiting_reasons': waiting_reasons,
                        'last_terminated': last_terminated,
                    })

        # Check deployments
        deps_result = self.get_deployments(all_namespaces=True)
        if deps_result['success']:
            for dep in deps_result.get('deployments', []):
                if dep.get('ready', 0) < dep.get('replicas', 1):
                    unhealthy['deployments'].append(dep)

        return {
            'success': True,
            'unhealthy_pods': unhealthy['pods'],
            'restarted_pods': unhealthy['restarted_pods'],
            'unhealthy_deployments': unhealthy['deployments'],
            'total_issues': (
                len(unhealthy['pods'])
                + len(unhealthy['deployments'])
                + len(unhealthy['restarted_pods'])
            )
        }

    # =========================================================================
    # Tool Schemas for LLM Function Calling
    # =========================================================================

    def get_schemas(self) -> List[Dict[str, Any]]:
        """
        Return tool schemas for LLM function calling.

        These tools enable CFOperator to troubleshoot K8s/K3s clusters.
        """
        return [
            {
                'name': 'k8s_get_pods',
                'description': 'List pods in a namespace or across all namespaces. Returns pod names, phase, and status.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'namespace': {
                            'type': 'string',
                            'description': 'Kubernetes namespace (default: "default")',
                            'default': 'default'
                        },
                        'labels': {
                            'type': 'string',
                            'description': 'Label selector (e.g., "app=nginx")'
                        },
                        'all_namespaces': {
                            'type': 'boolean',
                            'description': 'List pods in all namespaces',
                            'default': False
                        }
                    }
                }
            },
            {
                'name': 'k8s_get_pod_status',
                'description': 'Get detailed status of a specific pod including conditions, container statuses, and IPs.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'namespace': {'type': 'string', 'description': 'Kubernetes namespace'},
                        'pod_name': {'type': 'string', 'description': 'Name of the pod'}
                    },
                    'required': ['namespace', 'pod_name']
                }
            },
            {
                'name': 'k8s_get_pod_logs',
                'description': 'Get logs from a pod. Can tail, get previous container logs, or filter by time.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'namespace': {'type': 'string', 'description': 'Kubernetes namespace'},
                        'pod_name': {'type': 'string', 'description': 'Name of the pod'},
                        'container': {'type': 'string', 'description': 'Container name (required if pod has multiple containers)'},
                        'lines': {'type': 'integer', 'description': 'Number of lines to tail', 'default': 100},
                        'previous': {'type': 'boolean', 'description': 'Get logs from previous container instance', 'default': False},
                        'since': {'type': 'string', 'description': 'Only return logs newer than duration (e.g., "1h", "5m")'}
                    },
                    'required': ['namespace', 'pod_name']
                }
            },
            {
                'name': 'k8s_get_deployments',
                'description': 'List deployments showing replica counts and health status.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'namespace': {'type': 'string', 'description': 'Kubernetes namespace', 'default': 'default'},
                        'all_namespaces': {'type': 'boolean', 'description': 'List in all namespaces', 'default': False}
                    }
                }
            },
            {
                'name': 'k8s_rollout_status',
                'description': 'Get rollout status of a deployment.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'namespace': {'type': 'string', 'description': 'Kubernetes namespace'},
                        'deployment': {'type': 'string', 'description': 'Deployment name'}
                    },
                    'required': ['namespace', 'deployment']
                }
            },
            {
                'name': 'k8s_rollout_restart',
                'description': 'Restart a deployment by triggering a rolling update.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'namespace': {'type': 'string', 'description': 'Kubernetes namespace'},
                        'deployment': {'type': 'string', 'description': 'Deployment name'}
                    },
                    'required': ['namespace', 'deployment']
                }
            },
            {
                'name': 'k8s_get_services',
                'description': 'List services showing type, cluster IP, ports, and selectors.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'namespace': {'type': 'string', 'description': 'Kubernetes namespace', 'default': 'default'},
                        'all_namespaces': {'type': 'boolean', 'description': 'List in all namespaces', 'default': False}
                    }
                }
            },
            {
                'name': 'k8s_get_events',
                'description': 'Get cluster events. Useful for debugging pod failures, scheduling issues, etc.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'namespace': {'type': 'string', 'description': 'Kubernetes namespace', 'default': 'default'},
                        'resource_name': {'type': 'string', 'description': 'Filter events for a specific resource'},
                        'all_namespaces': {'type': 'boolean', 'description': 'Get events from all namespaces', 'default': False}
                    }
                }
            },
            {
                'name': 'k8s_describe',
                'description': 'Describe a Kubernetes resource in detail. Works for any resource type.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'resource_type': {'type': 'string', 'description': 'Type (pod, deployment, service, node, configmap, secret, etc.)'},
                        'name': {'type': 'string', 'description': 'Resource name'},
                        'namespace': {'type': 'string', 'description': 'Kubernetes namespace', 'default': 'default'}
                    },
                    'required': ['resource_type', 'name']
                }
            },
            {
                'name': 'k8s_get_nodes',
                'description': 'List all nodes in the cluster with status and system info.',
                'parameters': {
                    'type': 'object',
                    'properties': {}
                }
            },
            {
                'name': 'k8s_get_node_metrics',
                'description': 'Get CPU and memory usage metrics for all nodes (requires metrics-server).',
                'parameters': {
                    'type': 'object',
                    'properties': {}
                }
            },
            {
                'name': 'k8s_exec_pod',
                'description': 'Execute a command inside a pod. Use for debugging or collecting info.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'namespace': {'type': 'string', 'description': 'Kubernetes namespace'},
                        'pod_name': {'type': 'string', 'description': 'Name of the pod'},
                        'command': {'type': 'string', 'description': 'Command to execute'},
                        'container': {'type': 'string', 'description': 'Container name (if pod has multiple)'},
                        'timeout': {'type': 'integer', 'description': 'Command timeout in seconds', 'default': 30}
                    },
                    'required': ['namespace', 'pod_name', 'command']
                }
            },
            {
                'name': 'k8s_get_namespaces',
                'description': 'List all namespaces in the cluster.',
                'parameters': {
                    'type': 'object',
                    'properties': {}
                }
            },
            {
                'name': 'k8s_get_all_unhealthy',
                'description': 'Get unhealthy pods and deployments across the cluster, plus pods with restart history or recent waiting/termination state. Use this to catch recovered CrashLoopBackOff or readiness-failure cases that may look Running right now.',
                'parameters': {
                    'type': 'object',
                    'properties': {}
                }
            },
            {
                'name': 'k8s_get_cluster_info',
                'description': 'Get cluster information including control plane endpoint.',
                'parameters': {
                    'type': 'object',
                    'properties': {}
                }
            }
        ]
