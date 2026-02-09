"""Docker Container Backend Implementation"""
from typing import List, Dict, Any, Optional
import docker
from .base import ContainerBackend

class DockerContainers(ContainerBackend):
    """Docker implementation of ContainerBackend."""

    def __init__(self, hosts: Dict[str, str] = None):
        """
        Initialize Docker backend.

        Args:
            hosts: Dict mapping host names to Docker socket URLs
                   e.g., {'local': 'unix:///var/run/docker.sock',
                         'worker-1': 'tcp://worker-1:2375'}
        """
        self.hosts = hosts or {'local': 'unix:///var/run/docker.sock'}
        self.clients = {name: docker.DockerClient(base_url=url) for name, url in self.hosts.items()}

    def list_containers(self, host: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all containers across hosts."""
        containers = []
        targets = [host] if host else self.clients.keys()

        for h in targets:
            client = self.clients.get(h)
            if client:
                for c in client.containers.list(all=True):
                    containers.append({
                        'host': h,
                        'name': c.name,
                        'status': c.status,
                        'image': c.image.tags[0] if c.image.tags else 'unknown'
                    })
        return containers

    def inspect(self, container: str, host: Optional[str] = None) -> Dict[str, Any]:
        """Get container details."""
        client = self.clients.get(host or 'local')
        c = client.containers.get(container)
        return c.attrs

    def get_logs(self, container: str, tail: int = 100, since: Optional[str] = None, host: Optional[str] = None) -> str:
        """Get container logs."""
        client = self.clients.get(host or 'local')
        c = client.containers.get(container)
        return c.logs(tail=tail, since=since).decode('utf-8')

    def restart(self, container: str, host: Optional[str] = None) -> bool:
        """Restart container."""
        try:
            client = self.clients.get(host or 'local')
            c = client.containers.get(container)
            c.restart()
            return True
        except Exception:
            return False
