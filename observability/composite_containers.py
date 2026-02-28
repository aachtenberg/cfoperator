"""Composite Container Backend — aggregates multiple container backends."""
import logging
from typing import List, Dict, Any, Optional
from .base import ContainerBackend

logger = logging.getLogger("cfoperator.observability.composite")


class CompositeContainerBackend(ContainerBackend):
    """
    Fans out ContainerBackend calls to multiple backends and aggregates results.

    Mirrors how notifications works as a list — any combination of
    kubernetes, docker, prometheus backends can be combined.
    """

    def __init__(self, backends: List[ContainerBackend]):
        self.backends = backends

    def list_containers(self, host: Optional[str] = None) -> List[Dict[str, Any]]:
        """Aggregate containers from all backends."""
        all_containers = []
        for backend in self.backends:
            try:
                containers = backend.list_containers(host=host)
                all_containers.extend(containers)
            except Exception as e:
                logger.warning(f"Backend {type(backend).__name__} list_containers failed: {e}")
        return all_containers

    def inspect(self, container: str, host: Optional[str] = None) -> Dict[str, Any]:
        """Try each backend until one returns a result."""
        for backend in self.backends:
            try:
                result = backend.inspect(container, host=host)
                if result:
                    return result
            except Exception as e:
                logger.debug(f"Backend {type(backend).__name__} inspect failed: {e}")
        return {}

    def get_logs(self, container: str, tail: int = 100,
                 since: Optional[str] = None, host: Optional[str] = None) -> str:
        """Try each backend until one returns logs."""
        for backend in self.backends:
            try:
                logs = backend.get_logs(container, tail=tail, since=since, host=host)
                if logs:
                    return logs
            except Exception as e:
                logger.debug(f"Backend {type(backend).__name__} get_logs failed: {e}")
        return ''

    def restart(self, container: str, host: Optional[str] = None) -> bool:
        """Try each backend until one succeeds."""
        for backend in self.backends:
            try:
                if backend.restart(container, host=host):
                    return True
            except Exception as e:
                logger.debug(f"Backend {type(backend).__name__} restart failed: {e}")
        return False

    @property
    def runtime_names(self) -> List[str]:
        """Return human-readable names of active runtimes."""
        name_map = {
            'KubernetesContainers': 'kubernetes',
            'DockerContainers': 'docker',
            'PrometheusContainers': 'prometheus/docker',
        }
        return [name_map.get(type(b).__name__, type(b).__name__) for b in self.backends]
