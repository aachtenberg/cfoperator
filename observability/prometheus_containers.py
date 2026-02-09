"""Prometheus-based Container Discovery Implementation"""
from typing import List, Dict, Any, Optional
import subprocess
import requests
from .base import ContainerBackend


class PrometheusContainers(ContainerBackend):
    """
    Container backend that discovers containers via Prometheus metrics.
    Uses SSH for actions (restart, logs, etc.).

    This approach:
    - Queries Prometheus for container_last_seen metrics across all hosts
    - Uses SSH for container actions (no Docker TCP API needed)
    - Works with existing infrastructure (Prometheus + SSH keys)
    """

    def __init__(self, prometheus_url: str, ssh_user: str = 'aachten'):
        """
        Initialize Prometheus container backend.

        Args:
            prometheus_url: Prometheus URL (e.g., http://localhost:9090)
            ssh_user: SSH username for remote actions
        """
        self.prometheus_url = prometheus_url.rstrip('/')
        self.ssh_user = ssh_user

    def _query_prometheus(self, query: str) -> List[Dict[str, Any]]:
        """Query Prometheus and return results."""
        try:
            resp = requests.get(
                f'{self.prometheus_url}/api/v1/query',
                params={'query': query},
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get('data', {}).get('result', [])
        except Exception as e:
            print(f"Prometheus query error: {e}")
            return []

    def _ssh_docker_command(self, host: str, command: List[str]) -> str:
        """Execute docker command via SSH."""
        try:
            result = subprocess.run(
                ['ssh', f'{self.ssh_user}@{host}'] + command,
                capture_output=True,
                timeout=30,
                text=True
            )
            return result.stdout if result.returncode == 0 else ''
        except Exception as e:
            print(f"SSH command error: {e}")
            return ''

    def list_containers(self, host: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List all containers by querying Prometheus.

        Uses container_last_seen metric which is exported by cAdvisor/node-exporter.
        """
        # Query Prometheus for all containers across fleet
        query = 'container_last_seen{name!="", name!="POD"}'
        if host:
            query = f'container_last_seen{{name!="", name!="POD", instance=~".*{host}.*"}}'

        results = self._query_prometheus(query)

        containers = []
        for result in results:
            metric = result.get('metric', {})
            container_name = metric.get('name', metric.get('container_name', ''))
            instance = metric.get('instance', '')
            image = metric.get('image', '')

            # Extract host from instance label (format: hostname:port or hostname)
            host_name = instance.split(':')[0] if instance else 'unknown'

            # Check if container is running (value is timestamp, if recent = running)
            value = float(result.get('value', [0, 0])[1])
            import time
            is_running = (time.time() - value) < 120  # Consider running if seen in last 2 min

            containers.append({
                'host': host_name,
                'name': container_name,
                'status': 'running' if is_running else 'stopped',
                'image': image
            })

        return containers

    def inspect(self, container: str, host: Optional[str] = None) -> Dict[str, Any]:
        """Get container details via SSH + docker inspect."""
        if not host:
            # Find host from Prometheus
            containers = self.list_containers()
            for c in containers:
                if c['name'] == container:
                    host = c['host']
                    break
            if not host:
                return {}

        output = self._ssh_docker_command(host, ['docker', 'inspect', container])
        if output:
            import json
            try:
                return json.loads(output)[0]
            except:
                return {}
        return {}

    def get_logs(self, container: str, tail: int = 100, since: Optional[str] = None,
                 host: Optional[str] = None) -> str:
        """Get container logs via SSH + docker logs."""
        if not host:
            # Find host from Prometheus
            containers = self.list_containers()
            for c in containers:
                if c['name'] == container:
                    host = c['host']
                    break
            if not host:
                return ''

        cmd = ['docker', 'logs', '--tail', str(tail)]
        if since:
            cmd.extend(['--since', since])
        cmd.append(container)

        return self._ssh_docker_command(host, cmd)

    def restart(self, container: str, host: Optional[str] = None) -> bool:
        """Restart container via SSH + docker restart."""
        if not host:
            # Find host from Prometheus
            containers = self.list_containers()
            for c in containers:
                if c['name'] == container:
                    host = c['host']
                    break
            if not host:
                return False

        output = self._ssh_docker_command(host, ['docker', 'restart', container])
        return bool(output.strip())
