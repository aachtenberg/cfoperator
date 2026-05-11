"""Pluggable bare-metal host observability providers for the event runtime."""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import logging

from .models import Alert, ContextEnvelope, HostObservation, HostTarget
from .plugins import ContextProvider, HostObservabilityProvider
from .telemetry import observe_host_discovery, observe_host_observation

logger = logging.getLogger(__name__)


def _float_or_none(value: str | None) -> float | None:
    if value in (None, "", "nan"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: str | None) -> int | None:
    if value in (None, "", "nan"):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _percent(numerator: float | int | None, denominator: float | int | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return round((float(numerator) / float(denominator)) * 100.0, 2)


def _expand_env_vars(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand_env_vars(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env_vars(item) for item in value]
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.getenv(value[2:-1], "")
    return value


def _read_meminfo(proc_dir: Path) -> Dict[str, int]:
    values: Dict[str, int] = {}
    path = proc_dir / "meminfo"
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        parts = raw_value.strip().split()
        if not parts:
            continue
        try:
            values[key] = int(parts[0]) * 1024
        except ValueError:
            continue
    return values


def _local_root_stats(root_path: str) -> Dict[str, Any]:
    usage = shutil.disk_usage(root_path)
    return {
        "path": root_path,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "available_bytes": usage.free,
        "used_percent": _percent(usage.used, usage.total),
    }


def _local_stats(root_path: str = "/", proc_dir: str = "/proc") -> Dict[str, Any]:
    proc_path = Path(proc_dir)
    meminfo = _read_meminfo(proc_path)
    uptime_seconds = None
    uptime_path = proc_path / "uptime"
    if uptime_path.exists():
        try:
            uptime_seconds = float(uptime_path.read_text(encoding="utf-8").split()[0])
        except (IndexError, ValueError):
            uptime_seconds = None

    load_average = None
    if hasattr(os, "getloadavg"):
        try:
            load_average = os.getloadavg()
        except OSError:
            load_average = None

    hostname = socket.gethostname()
    fqdn = socket.getfqdn()
    address = None
    try:
        address = socket.gethostbyname(hostname)
    except OSError:
        address = None

    total_memory = meminfo.get("MemTotal")
    available_memory = meminfo.get("MemAvailable")
    used_memory = None
    if total_memory is not None and available_memory is not None:
        used_memory = total_memory - available_memory

    return {
        "hostname": hostname,
        "fqdn": fqdn,
        "address": address,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "uptime_seconds": uptime_seconds,
        "cpu": {
            "logical_cores": os.cpu_count() or 0,
            "load_average_1m": load_average[0] if load_average else None,
            "load_average_5m": load_average[1] if load_average else None,
            "load_average_15m": load_average[2] if load_average else None,
        },
        "memory": {
            "total_bytes": total_memory,
            "available_bytes": available_memory,
            "used_bytes": used_memory,
            "used_percent": _percent(used_memory, total_memory),
        },
        "disk": {
            "root": _local_root_stats(root_path),
        },
    }


class LocalHostStatsProvider(HostObservabilityProvider):
    """Collect bare-metal OS stats from the local machine with stdlib only."""

    name = "local-host-stats"

    def __init__(self, root_path: str = "/", proc_dir: str = "/proc"):
        self.root_path = root_path
        self.proc_dir = proc_dir

    def discover_targets(self) -> List[HostTarget]:
        stats = _local_stats(root_path=self.root_path, proc_dir=self.proc_dir)
        aliases = [item for item in {stats.get("hostname"), stats.get("fqdn"), stats.get("address")} if item]
        return [
            HostTarget(
                name=str(stats.get("hostname") or "localhost"),
                provider=self.name,
                address=stats.get("address"),
                aliases=aliases,
                metadata={"role": "local"},
            )
        ]

    def collect(self, target: HostTarget) -> HostObservation:
        return HostObservation(provider=self.name, target=target.name, stats=_local_stats(self.root_path, self.proc_dir))


class SSHHostStatsProvider(HostObservabilityProvider):
    """Collect bare-metal host stats from configured SSH targets."""

    name = "ssh-host-stats"

    def __init__(
        self,
        targets: Iterable[HostTarget],
        connect_timeout: int = 5,
        command_timeout: int = 15,
        strict_host_key_checking: bool = False,
    ):
        self.targets = list(targets)
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.strict_host_key_checking = strict_host_key_checking

    def discover_targets(self) -> List[HostTarget]:
        return list(self.targets)

    def collect(self, target: HostTarget) -> HostObservation | None:
        address = target.address or target.metadata.get("address")
        user = target.metadata.get("user")
        if not address or not user:
            return None

        ssh_cmd = [
            "ssh",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
        ]
        if not self.strict_host_key_checking:
            # Homelab default: favor reachability over trust-on-first-use prompts.
            ssh_cmd.extend([
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
            ])
        port = target.metadata.get("port")
        if port:
            ssh_cmd.extend(["-p", str(port)])
        key_path = target.metadata.get("key_path")
        if key_path:
            ssh_cmd.extend(["-i", os.path.expanduser(str(key_path))])
        ssh_cmd.append(f"{user}@{address}")
        ssh_cmd.append("sh")

        remote_script = """
hostname=$(hostname 2>/dev/null || echo unknown)
load_1m=$(cut -d' ' -f1 /proc/loadavg 2>/dev/null || true)
load_5m=$(cut -d' ' -f2 /proc/loadavg 2>/dev/null || true)
load_15m=$(cut -d' ' -f3 /proc/loadavg 2>/dev/null || true)
uptime_seconds=$(cut -d' ' -f1 /proc/uptime 2>/dev/null || true)
cpu_cores=$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 0)

printf 'hostname=%s\n' "$hostname"
printf 'load_1m=%s\n' "$load_1m"
printf 'load_5m=%s\n' "$load_5m"
printf 'load_15m=%s\n' "$load_15m"
printf 'uptime_seconds=%s\n' "$uptime_seconds"
printf 'cpu_cores=%s\n' "$cpu_cores"

awk '/MemTotal:/ {print "mem_total_bytes=" $2 * 1024} /MemAvailable:/ {print "mem_available_bytes=" $2 * 1024}' /proc/meminfo 2>/dev/null
df -Pk / 2>/dev/null | awk 'NR==2 {print "disk_total_bytes=" $2 * 1024; print "disk_used_bytes=" $3 * 1024; print "disk_available_bytes=" $4 * 1024}'
""".strip()

        try:
            result = subprocess.run(
                ssh_cmd,
                input=remote_script,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

        if result.returncode != 0:
            return None

        values: Dict[str, str] = {}
        for line in result.stdout.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()

        total_memory = _int_or_none(values.get("mem_total_bytes"))
        available_memory = _int_or_none(values.get("mem_available_bytes"))
        used_memory = None if total_memory is None or available_memory is None else total_memory - available_memory
        disk_total = _int_or_none(values.get("disk_total_bytes"))
        disk_used = _int_or_none(values.get("disk_used_bytes"))
        disk_available = _int_or_none(values.get("disk_available_bytes"))

        return HostObservation(
            provider=self.name,
            target=target.name,
            stats={
                "hostname": values.get("hostname") or target.name,
                "address": address,
                "uptime_seconds": _float_or_none(values.get("uptime_seconds")),
                "cpu": {
                    "logical_cores": _int_or_none(values.get("cpu_cores")) or 0,
                    "load_average_1m": _float_or_none(values.get("load_1m")),
                    "load_average_5m": _float_or_none(values.get("load_5m")),
                    "load_average_15m": _float_or_none(values.get("load_15m")),
                },
                "memory": {
                    "total_bytes": total_memory,
                    "available_bytes": available_memory,
                    "used_bytes": used_memory,
                    "used_percent": _percent(used_memory, total_memory),
                },
                "disk": {
                    "root": {
                        "path": "/",
                        "total_bytes": disk_total,
                        "used_bytes": disk_used,
                        "available_bytes": disk_available,
                        "used_percent": _percent(disk_used, disk_total),
                    }
                },
            },
        )


class PrometheusHostStatsProvider(HostObservabilityProvider):
    """Discover and collect host OS stats from Prometheus node-exporter metrics."""

    name = "prometheus-host-stats"

    def __init__(
        self,
        url: str,
        job_pattern: str = "node-exporter|node_exporter",
        discover: bool = True,
        targets: Iterable[HostTarget] | None = None,
        timeout_seconds: int = 10,
    ):
        self.url = url.rstrip("/")
        self.job_pattern = job_pattern
        self.discover = discover
        self.targets = list(targets or [])
        self.timeout_seconds = timeout_seconds

    def discover_targets(self) -> List[HostTarget]:
        targets = list(self.targets)
        if not self.discover:
            return targets

        payload = self._query(f'up{{job=~"{self.job_pattern}"}} == 1')
        for item in payload.get("data", {}).get("result", []):
            metric = item.get("metric", {})
            instance = str(metric.get("instance") or "")
            host = str(metric.get("nodename") or metric.get("hostname") or instance.split(":", 1)[0] or instance)
            if not host:
                continue
            aliases = [value for value in {instance, instance.split(":", 1)[0], metric.get("hostname"), metric.get("nodename")} if value]
            target = HostTarget(
                name=host,
                provider=self.name,
                address=instance.split(":", 1)[0] if instance else None,
                aliases=aliases,
                metadata={"instance": instance, "job": metric.get("job")},
            )
            if not any(existing.name == target.name and existing.address == target.address for existing in targets):
                targets.append(target)
        return targets

    def collect(self, target: HostTarget) -> HostObservation | None:
        instance = str(target.metadata.get("instance") or target.address or target.name)
        if not instance:
            return None

        labels = self._instance_selector(instance)
        load_1m = self._scalar(f"node_load1{{{labels}}}")
        uptime_seconds = self._scalar(f"node_time_seconds{{{labels}}} - node_boot_time_seconds{{{labels}}}")
        cpu_busy = self._scalar(
            "100 * (1 - avg(rate(node_cpu_seconds_total"
            f"{{mode=\"idle\",{labels}}}[5m])))"
        )
        total_memory = self._scalar(f"node_memory_MemTotal_bytes{{{labels}}}")
        available_memory = self._scalar(f"node_memory_MemAvailable_bytes{{{labels}}}")
        filesystem_labels = f'{labels},mountpoint="/",fstype!~"tmpfs|overlay|squashfs"'
        disk_total = self._scalar(f"node_filesystem_size_bytes{{{filesystem_labels}}}")
        disk_available = self._scalar(f"node_filesystem_avail_bytes{{{filesystem_labels}}}")
        used_memory = None if total_memory is None or available_memory is None else total_memory - available_memory
        disk_used = None if disk_total is None or disk_available is None else disk_total - disk_available

        return HostObservation(
            provider=self.name,
            target=target.name,
            stats={
                "hostname": target.name,
                "address": target.address,
                "uptime_seconds": uptime_seconds,
                "cpu": {
                    "logical_cores": None,
                    "utilization_percent_5m": round(cpu_busy, 2) if cpu_busy is not None else None,
                    "load_average_1m": load_1m,
                },
                "memory": {
                    "total_bytes": total_memory,
                    "available_bytes": available_memory,
                    "used_bytes": used_memory,
                    "used_percent": _percent(used_memory, total_memory),
                },
                "disk": {
                    "root": {
                        "path": "/",
                        "total_bytes": disk_total,
                        "used_bytes": disk_used,
                        "available_bytes": disk_available,
                        "used_percent": _percent(disk_used, disk_total),
                    }
                },
            },
        )

    def _instance_selector(self, instance: str) -> str:
        escaped = instance.replace("\\", "\\\\").replace('"', '\\"')
        return f'instance="{escaped}"'

    def _query(self, query: str) -> Dict[str, Any]:
        url = f"{self.url}/api/v1/query?{urlencode({'query': query})}"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
            return {"status": "error", "data": {"result": []}}

    def _scalar(self, query: str) -> float | None:
        payload = self._query(query)
        results = payload.get("data", {}).get("result", [])
        if not results:
            return None
        value = results[0].get("value", [None, None])[1]
        return _float_or_none(str(value) if value is not None else None)


class PrometheusK3sProvider(HostObservabilityProvider):
    """Discover k3s cluster nodes and collect workload stats via Prometheus.

    Queries kube-state-metrics and cAdvisor metrics already scraped by
    Prometheus to surface node conditions, pod counts, restart storms,
    and per-node resource usage.
    """

    name = "prometheus-k3s"

    def __init__(
        self,
        url: str,
        discover: bool = True,
        targets: Iterable[HostTarget] | None = None,
        timeout_seconds: int = 10,
    ):
        self.url = url.rstrip("/")
        self.discover = discover
        self.targets = list(targets or [])
        self.timeout_seconds = timeout_seconds

    def discover_targets(self) -> List[HostTarget]:
        targets = list(self.targets)
        if not self.discover:
            return targets

        payload = self._query("kube_node_info")
        for item in payload.get("data", {}).get("result", []):
            metric = item.get("metric", {})
            node = str(metric.get("node") or "")
            if not node:
                continue
            address = str(metric.get("internal_ip") or "")
            aliases = [value for value in {node, address, metric.get("system_uuid")} if value]
            metadata: Dict[str, Any] = {
                "kernel_version": metric.get("kernel_version"),
                "os_image": metric.get("os_image"),
                "container_runtime_version": metric.get("container_runtime_version"),
                "kubelet_version": metric.get("kubelet_version"),
            }
            metadata = {k: v for k, v in metadata.items() if v}
            target = HostTarget(
                name=node,
                provider=self.name,
                address=address or None,
                aliases=aliases,
                metadata=metadata,
            )
            if not any(existing.name == target.name for existing in targets):
                targets.append(target)
        return targets

    def collect(self, target: HostTarget) -> HostObservation | None:
        node = target.name
        if not node:
            return None

        node_sel = self._node_selector(node)

        # Node conditions
        conditions = self._node_conditions(node_sel)

        # Pod counts by namespace
        pod_counts = self._vector_map(
            f'count(kube_pod_info{{{node_sel}}}) by (namespace)',
            label_key="namespace",
        )

        # Pod phases
        pod_phases = self._vector_map(
            f'count(kube_pod_status_phase{{{node_sel},phase=~"Running|Pending|Failed|Succeeded|Unknown"}}) by (phase)',
            label_key="phase",
        )

        # Container restarts (top offenders)
        restart_results = self._query(
            f'topk(10, sum(kube_pod_container_status_restarts_total{{{node_sel}}}) by (namespace, pod))'
        )
        restarts = []
        for item in restart_results.get("data", {}).get("result", []):
            metric = item.get("metric", {})
            value = _float_or_none(str(item.get("value", [None, None])[1]))
            if value is not None and value > 0:
                restarts.append({
                    "namespace": metric.get("namespace"),
                    "pod": metric.get("pod"),
                    "restarts": int(value),
                })

        # CPU usage (cAdvisor, summed per node)
        cpu_usage_cores = self._scalar(
            f'sum(rate(container_cpu_usage_seconds_total{{{node_sel},container!=""}}[5m]))'
        )

        # Memory usage (cAdvisor, summed per node)
        memory_usage_bytes = self._scalar(
            f'sum(container_memory_working_set_bytes{{{node_sel},container!=""}})'
        )

        # Node allocatable resources
        cpu_allocatable = self._scalar(
            f'kube_node_status_allocatable{{{node_sel},resource="cpu"}}'
        )
        memory_allocatable = self._scalar(
            f'kube_node_status_allocatable{{{node_sel},resource="memory"}}'
        )

        return HostObservation(
            provider=self.name,
            target=node,
            stats={
                "node": node,
                "conditions": conditions,
                "pods": {
                    "by_namespace": pod_counts,
                    "by_phase": pod_phases,
                    "total": sum(int(v) for v in pod_counts.values()) if pod_counts else 0,
                },
                "restarts": restarts,
                "resources": {
                    "cpu": {
                        "usage_cores": round(cpu_usage_cores, 4) if cpu_usage_cores is not None else None,
                        "allocatable_cores": cpu_allocatable,
                        "utilization_percent": _percent(cpu_usage_cores, cpu_allocatable),
                    },
                    "memory": {
                        "usage_bytes": memory_usage_bytes,
                        "allocatable_bytes": memory_allocatable,
                        "utilization_percent": _percent(memory_usage_bytes, memory_allocatable),
                    },
                },
            },
        )

    def _node_selector(self, node: str) -> str:
        escaped = node.replace("\\", "\\\\").replace('"', '\\"')
        return f'node="{escaped}"'

    def _node_conditions(self, node_sel: str) -> Dict[str, bool]:
        conditions: Dict[str, bool] = {}
        for condition in ("Ready", "MemoryPressure", "DiskPressure", "PIDPressure"):
            value = self._scalar(
                f'kube_node_status_condition{{{node_sel},condition="{condition}",status="true"}}'
            )
            if value is not None:
                conditions[condition] = value == 1.0
        return conditions

    def _vector_map(self, query: str, label_key: str) -> Dict[str, int]:
        payload = self._query(query)
        result: Dict[str, int] = {}
        for item in payload.get("data", {}).get("result", []):
            key = str(item.get("metric", {}).get(label_key) or "unknown")
            value = _float_or_none(str(item.get("value", [None, None])[1]))
            if value is not None:
                result[key] = int(value)
        return result

    def _query(self, query: str) -> Dict[str, Any]:
        url = f"{self.url}/api/v1/query?{urlencode({'query': query})}"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
            return {"status": "error", "data": {"result": []}}

    def _scalar(self, query: str) -> float | None:
        payload = self._query(query)
        results = payload.get("data", {}).get("result", [])
        if not results:
            return None
        value = results[0].get("value", [None, None])[1]
        return _float_or_none(str(value) if value is not None else None)


class BareMetalHostContextProvider(ContextProvider):
    """Aggregate configured and discovered bare-metal host stats into alert context."""

    name = "baremetal-host-context"
    capabilities = ("host", "baremetal", "os_stats", "discovery")

    def __init__(
        self,
        providers: Iterable[HostObservabilityProvider],
        default_to_local: bool = True,
        include_discovered_targets: bool = True,
        refresh_interval_seconds: int = 300,
    ):
        self.providers = list(providers)
        self.default_to_local = default_to_local
        self.include_discovered_targets = include_discovered_targets
        self.refresh_interval_seconds = max(0, int(refresh_interval_seconds))
        self._provider_by_name = {provider.name: provider for provider in self.providers}
        self._discovered_targets: List[HostTarget] = []
        self._last_refresh_monotonic: float | None = None

    def start(self) -> None:
        self.refresh_targets(force=True)

    def refresh_targets(self, force: bool = False) -> None:
        if not force and not self._needs_refresh():
            return
        targets: List[HostTarget] = []
        for provider in self.providers:
            try:
                discovered = provider.discover_targets()
            except Exception:
                logger.warning("Host discovery failed for provider %s", provider.name, exc_info=True)
                observe_host_discovery(provider.name, "error", targets=0)
                continue
            observe_host_discovery(provider.name, "success", targets=len(discovered), timestamp_seconds=time.time())
            for target in discovered:
                if not any(
                    existing.provider == target.provider and existing.name == target.name and existing.address == target.address
                    for existing in targets
                ):
                    targets.append(target)
        self._discovered_targets = targets
        self._last_refresh_monotonic = time.monotonic()

    def provide(self, alert: Alert, envelope: ContextEnvelope) -> ContextEnvelope:
        if not self._discovered_targets or self._needs_refresh():
            self.refresh_targets(force=not self._discovered_targets)

        hints = self._host_hints(alert)
        matches = self._match_targets(hints)
        if not matches and not hints and self.default_to_local:
            matches = [target for target in self._discovered_targets if target.metadata.get("role") == "local"]

        if self.include_discovered_targets:
            envelope.context.setdefault("discovery", {})["baremetal_targets"] = [
                target.to_dict() for target in self._discovered_targets
            ]

        observations = []
        for target in matches:
            provider = self._provider_by_name.get(target.provider)
            if provider is None:
                continue
            try:
                observation = provider.collect(target)
            except Exception:
                logger.warning("Host observation failed for %s/%s", target.provider, target.name, exc_info=True)
                observe_host_observation(target.provider, "error")
                continue
            if observation is not None:
                observe_host_observation(target.provider, "success")
                observations.append(observation.to_dict())
            else:
                observe_host_observation(target.provider, "error")

        if observations:
            envelope.context["host_observability"] = {
                "requested_targets": hints,
                "matched_targets": [target.to_dict() for target in matches],
                "observations": observations,
            }
        elif hints:
            envelope.notes.append(f"No bare-metal observability target matched: {', '.join(hints)}")

        return envelope

    def _needs_refresh(self) -> bool:
        if self._last_refresh_monotonic is None:
            return True
        if self.refresh_interval_seconds == 0:
            return True
        return (time.monotonic() - self._last_refresh_monotonic) >= self.refresh_interval_seconds

    def _host_hints(self, alert: Alert) -> List[str]:
        details = alert.details
        hints = [
            details.get("host"),
            details.get("hostname"),
            details.get("address"),
            details.get("instance"),
        ]
        if alert.resource_type == "host" and alert.resource_name:
            hints.append(alert.resource_name)
        normalized = []
        seen = set()
        for item in hints:
            if item is None:
                continue
            value = str(item).strip()
            if not value:
                continue
            lowered = value.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            normalized.append(value)
        return normalized

    def _match_targets(self, hints: List[str]) -> List[HostTarget]:
        if not hints:
            return []
        normalized_hints = {hint.lower() for hint in hints}
        matches: List[HostTarget] = []
        for target in self._discovered_targets:
            candidates = {target.name.lower()}
            if target.address:
                candidates.add(target.address.lower())
            for alias in target.aliases:
                candidates.add(alias.lower())
            if normalized_hints & candidates:
                matches.append(target)
        return matches


def build_host_targets(hosts: Any, provider_name: str) -> List[HostTarget]:
    """Build host targets from flexible list or mapping configuration."""
    if not hosts:
        return []

    targets: List[HostTarget] = []
    if isinstance(hosts, dict):
        items = []
        for name, value in hosts.items():
            if isinstance(value, str):
                items.append({"name": name, "address": value})
            elif isinstance(value, dict):
                merged = dict(value)
                merged.setdefault("name", name)
                items.append(merged)
        hosts = items

    if not isinstance(hosts, list):
        return []

    for item in hosts:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("hostname") or item.get("address") or "").strip()
        if not name:
            continue
        ssh_config = item.get("ssh") if isinstance(item.get("ssh"), dict) else {}
        metadata = {
            "user": item.get("user") or ssh_config.get("user"),
            "key_path": item.get("key_path") or ssh_config.get("key_path"),
            "port": item.get("port") or ssh_config.get("port"),
            "address": item.get("address"),
            "instance": item.get("instance"),
        }
        metadata.update({key: value for key, value in item.get("metadata", {}).items()}) if isinstance(item.get("metadata"), dict) else None
        aliases = [str(alias) for alias in item.get("aliases", []) if str(alias).strip()]
        if item.get("address") and item.get("address") not in aliases:
            aliases.append(str(item["address"]))
        target = HostTarget(
            name=name,
            provider=provider_name,
            address=item.get("address"),
            aliases=aliases,
            metadata={key: value for key, value in metadata.items() if value not in (None, "")},
        )
        targets.append(target)
    return targets


def load_host_observability_config_from_env() -> Dict[str, Any]:
    """Load portable host observability config from JSON env or JSON file."""
    raw_config = os.getenv("CFOP_EVENT_RUNTIME_HOST_OBSERVABILITY_JSON", "").strip()
    config_path = os.getenv("CFOP_EVENT_RUNTIME_HOST_OBSERVABILITY_CONFIG_PATH", "").strip()
    if raw_config:
        try:
            return json.loads(raw_config)
        except json.JSONDecodeError:
            return {}
    if config_path:
        path = Path(os.path.expanduser(config_path))
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
    return {}


def load_host_observability_config_from_yaml(config_path: str | None = None) -> Dict[str, Any]:
    """Load host observability config from the repository YAML config when available."""
    candidate = config_path or os.getenv("CONFIG_PATH", "config.yaml")
    if not candidate:
        return {}
    path = Path(os.path.expanduser(candidate))
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore
    except ImportError:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
    except Exception as exc:
        logger.warning("Failed to load host observability YAML config from %s: %s", path, exc)
        return {}
    payload = _expand_env_vars(payload)
    if not isinstance(payload, dict):
        return {}

    runtime_config = payload.get("event_runtime") if isinstance(payload.get("event_runtime"), dict) else {}
    if isinstance(runtime_config.get("host_observability"), dict):
        return dict(runtime_config["host_observability"])

    observability_config = payload.get("observability") if isinstance(payload.get("observability"), dict) else {}
    if isinstance(observability_config.get("host_observability"), dict):
        return dict(observability_config["host_observability"])
    return {}


def load_host_observability_config(config_path: str | None = None) -> Dict[str, Any]:
    """Load host observability config from env-first, YAML-second sources."""
    config = load_host_observability_config_from_env()
    if config:
        return config
    return load_host_observability_config_from_yaml(config_path=config_path)


def build_host_observability_plugins(
    config: Dict[str, Any] | None = None,
    config_path: str | None = None,
) -> tuple[List[HostObservabilityProvider], BareMetalHostContextProvider | None]:
    """Build configured bare-metal host observability providers and context provider."""
    enabled = os.getenv("CFOP_EVENT_RUNTIME_HOST_OBSERVABILITY_ENABLED", "1").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return [], None

    if config is None:
        config = load_host_observability_config(config_path=config_path)

    provider_specs = config.get("providers") if isinstance(config, dict) else None
    if not isinstance(provider_specs, list) or not provider_specs:
        provider_specs = [{"type": "local"}]

    providers: List[HostObservabilityProvider] = []
    for spec in provider_specs:
        if not isinstance(spec, dict):
            continue
        provider_type = str(spec.get("type") or spec.get("backend") or "").strip().lower()
        if provider_type == "local":
            providers.append(
                LocalHostStatsProvider(
                    root_path=str(spec.get("root_path") or "/"),
                    proc_dir=str(spec.get("proc_dir") or "/proc"),
                )
            )
        elif provider_type == "ssh":
            targets = build_host_targets(spec.get("hosts"), SSHHostStatsProvider.name)
            if targets:
                providers.append(
                    SSHHostStatsProvider(
                        targets=targets,
                        connect_timeout=int(spec.get("connect_timeout") or 5),
                        command_timeout=int(spec.get("command_timeout") or 15),
                        strict_host_key_checking=bool(spec.get("strict_host_key_checking", False)),
                    )
                )
        elif provider_type == "prometheus":
            url = str(spec.get("url") or "").strip()
            if not url:
                continue
            providers.append(
                PrometheusHostStatsProvider(
                    url=url,
                    job_pattern=str(spec.get("job_pattern") or "node-exporter|node_exporter"),
                    discover=bool(spec.get("discover", True)),
                    targets=build_host_targets(spec.get("hosts"), PrometheusHostStatsProvider.name),
                    timeout_seconds=int(spec.get("timeout") or 10),
                )
            )
        elif provider_type == "k3s":
            url = str(spec.get("url") or "").strip()
            if not url:
                continue
            providers.append(
                PrometheusK3sProvider(
                    url=url,
                    discover=bool(spec.get("discover", True)),
                    targets=build_host_targets(spec.get("hosts"), PrometheusK3sProvider.name),
                    timeout_seconds=int(spec.get("timeout") or 10),
                )
            )

    if not providers:
        return [], None

    context_provider = BareMetalHostContextProvider(
        providers=providers,
        default_to_local=bool(config.get("default_to_local", True)) if isinstance(config, dict) else True,
        include_discovered_targets=bool(config.get("include_discovered_targets", True)) if isinstance(config, dict) else True,
        refresh_interval_seconds=int(config.get("refresh_interval_seconds", 300)) if isinstance(config, dict) else 300,
    )
    return providers, context_provider