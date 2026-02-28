"""Kubernetes Container Backend Implementation"""
import json
import subprocess
import logging
from typing import List, Dict, Any, Optional
from .base import ContainerBackend

logger = logging.getLogger("cfoperator.observability.k8s_containers")


class KubernetesContainers(ContainerBackend):
    """
    Container backend that discovers pods via kubectl.

    Maps Kubernetes pods to the ContainerBackend interface:
    - host → namespace
    - name → pod name
    - status → pod phase (mapped to running/stopped/pending)
    - image → first container image
    """

    def __init__(self, kubeconfig: Optional[str] = None, context: Optional[str] = None):
        """
        Initialize Kubernetes container backend.

        Args:
            kubeconfig: Optional path to kubeconfig file
            context: Optional kubectl context to use
        """
        self.kubeconfig = kubeconfig
        self.context = context

    def _kubectl_cmd(self, args: List[str]) -> List[str]:
        """Build kubectl command with optional config."""
        cmd = ['kubectl']
        if self.kubeconfig:
            cmd.extend(['--kubeconfig', self.kubeconfig])
        if self.context:
            cmd.extend(['--context', self.context])
        cmd.extend(args)
        return cmd

    def _run_kubectl(self, args: List[str], timeout: int = 30) -> subprocess.CompletedProcess:
        """Execute kubectl command and return CompletedProcess."""
        cmd = self._kubectl_cmd(args)
        logger.debug(f"Running: {' '.join(cmd)}")
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )

    @staticmethod
    def _phase_to_status(phase: str) -> str:
        """Map Kubernetes pod phase to container status string."""
        return {
            'Running': 'running',
            'Succeeded': 'running',  # completed jobs are "healthy"
            'Pending': 'pending',
            'Failed': 'stopped',
            'Unknown': 'stopped',
        }.get(phase, 'stopped')

    def list_containers(self, host: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List all pods across all namespaces.

        Args:
            host: If provided, treated as namespace filter.
        """
        args = ['get', 'pods', '-o', 'json']
        if host:
            args.extend(['-n', host])
        else:
            args.append('-A')

        try:
            result = self._run_kubectl(args)
            if result.returncode != 0:
                logger.warning(f"kubectl get pods failed: {result.stderr.strip()}")
                return []

            data = json.loads(result.stdout)
            pods = []
            for item in data.get('items', []):
                meta = item.get('metadata', {})
                status = item.get('status', {})
                spec = item.get('spec', {})
                phase = status.get('phase', 'Unknown')

                # Get first container image
                containers = spec.get('containers', [])
                image = containers[0].get('image', 'unknown') if containers else 'unknown'

                pods.append({
                    'host': meta.get('namespace', 'default'),
                    'name': meta.get('name', ''),
                    'status': self._phase_to_status(phase),
                    'image': image,
                    'runtime': 'kubernetes',
                })
            return pods
        except subprocess.TimeoutExpired:
            logger.error("kubectl get pods timed out")
            return []
        except Exception as e:
            logger.error(f"Error listing k8s pods: {e}")
            return []

    def inspect(self, container: str, host: Optional[str] = None) -> Dict[str, Any]:
        """
        Get detailed pod information.

        Args:
            container: Pod name
            host: Namespace (defaults to 'default')
        """
        namespace = host or 'default'
        args = ['get', 'pod', container, '-n', namespace, '-o', 'json']
        try:
            result = self._run_kubectl(args)
            if result.returncode != 0:
                return {}
            return json.loads(result.stdout)
        except Exception as e:
            logger.error(f"Error inspecting pod {container}: {e}")
            return {}

    def get_logs(self, container: str, tail: int = 100,
                 since: Optional[str] = None, host: Optional[str] = None) -> str:
        """
        Get pod logs.

        Args:
            container: Pod name
            tail: Number of lines
            since: Time filter (e.g., '1h')
            host: Namespace (defaults to 'default')
        """
        namespace = host or 'default'
        args = ['logs', container, '-n', namespace, f'--tail={tail}']
        if since:
            args.append(f'--since={since}')

        try:
            result = self._run_kubectl(args, timeout=60)
            return result.stdout if result.returncode == 0 else ''
        except Exception as e:
            logger.error(f"Error getting logs for pod {container}: {e}")
            return ''

    def restart(self, container: str, host: Optional[str] = None) -> bool:
        """
        Restart a pod by deleting it (controller recreates it).

        Args:
            container: Pod name
            host: Namespace (defaults to 'default')
        """
        namespace = host or 'default'
        args = ['delete', 'pod', container, '-n', namespace]
        try:
            result = self._run_kubectl(args, timeout=30)
            return result.returncode == 0
        except Exception as e:
            logger.error(f"Error restarting pod {container}: {e}")
            return False
