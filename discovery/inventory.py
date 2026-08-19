"""Deterministic enumeration of the fleet — facts come from APIs, never the model.

This is the first of the two strictly-ordered discovery layers (CFOP-47): a
fixed, bounded set of read-only queries against Prometheus and (optionally) the
Kubernetes API, producing a facts document the LLM then interprets. Every HTTP
request made is appended to a query log the final report prints verbatim, so an
operator can verify the "visibly tame" claim rather than take it on faith.

Sources:
  Prometheus (required)   PROMETHEUS_URL — targets, build info, a fixed set of
                          instant queries (node inventory, kube counts, restart
                          baselines).
  Kubernetes (optional)   in-cluster service account when running as a pod, or
                          CFOP_DISCOVERY_K8S_URL + CFOP_DISCOVERY_K8S_TOKEN —
                          read-only GETs: nodes, namespaces, workload counts.

Deliberately absent: SSH (never, per the discovery bounds), Loki and Docker
enumeration (additive later — Prometheus + k8s covers the demo and the
bare-Prometheus trial hook). Stdlib only.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# Bounds: enumeration must stay a bounded pass, not a crawler. Caps are on
# result size, not request count — the request list is fixed above.
MAX_SERIES_PER_QUERY = 200
MAX_TARGETS = 500

# The fixed instant-query set. Names are stable keys the prompt and report use.
PROM_QUERIES: Tuple[Tuple[str, str], ...] = (
    ("node_uname", 'node_uname_info'),
    ("node_mem_total_bytes", 'node_memory_MemTotal_bytes'),
    ("node_cpu_count", 'count by (instance) (node_cpu_seconds_total{mode="idle"})'),
    ("kube_node_info", 'kube_node_info'),
    ("pods_per_namespace", 'count by (namespace) (kube_pod_info)'),
    ("restarts_per_namespace", 'sum by (namespace) (kube_pod_container_status_restarts_total)'),
    ("prometheus_build", 'prometheus_build_info'),
)

_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class QueryLog:
    """Ordered record of every request the pass made, for the report appendix."""

    def __init__(self):
        self.entries: List[Dict[str, str]] = []

    def record(self, source: str, what: str, status: str) -> None:
        self.entries.append({"source": source, "what": what, "status": status})


def _get_json(url: str, log: QueryLog, source: str, what: str,
              headers: Optional[Dict[str, str]] = None,
              context: Optional[ssl.SSLContext] = None,
              timeout: int = 30) -> Optional[Any]:
    """GET a JSON document; failures are logged and returned as None, never
    raised — a missing source degrades the report, it must not kill the pass."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:  # nosec - operator-config URL
            data = json.loads(resp.read().decode("utf-8"))
        log.record(source, what, "ok")
        return data
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.record(source, what, f"failed: {e}")
        return None


# ---- Prometheus --------------------------------------------------------------


def _summarize_targets(payload: Any) -> Dict[str, Any]:
    """Compress /api/v1/targets into per-job counts + sample instances."""
    jobs: Dict[str, Dict[str, Any]] = {}
    active = (payload or {}).get("data", {}).get("activeTargets", []) or []
    for t in active[:MAX_TARGETS]:
        labels = t.get("labels", {}) or {}
        job = labels.get("job", "<unlabeled>")
        entry = jobs.setdefault(job, {"targets": 0, "up": 0, "down": 0, "instances": []})
        entry["targets"] += 1
        entry["up" if t.get("health") == "up" else "down"] += 1
        if len(entry["instances"]) < 5:
            entry["instances"].append(labels.get("instance", "?"))
    return {"job_count": len(jobs), "jobs": jobs, "truncated": len(active) > MAX_TARGETS}


def _summarize_instant(payload: Any) -> List[Dict[str, Any]]:
    """Compress an instant-query result to metric labels + value per series."""
    out: List[Dict[str, Any]] = []
    results = (payload or {}).get("data", {}).get("result", []) or []
    for series in results[:MAX_SERIES_PER_QUERY]:
        out.append({"labels": series.get("metric", {}), "value": (series.get("value") or [None, None])[1]})
    return out


def instant_query(prom_url: str, promql: str, log: QueryLog) -> List[Dict[str, Any]]:
    """One instant query — also the only follow-up the LLM may request."""
    url = f"{prom_url.rstrip('/')}/api/v1/query?query={urllib.parse.quote(promql)}"
    return _summarize_instant(_get_json(url, log, "prometheus", f"query: {promql}"))


def enumerate_prometheus(prom_url: str, log: QueryLog) -> Dict[str, Any]:
    base = prom_url.rstrip("/")
    facts: Dict[str, Any] = {
        "targets": _summarize_targets(
            _get_json(f"{base}/api/v1/targets", log, "prometheus", "GET /api/v1/targets")),
        "queries": {},
    }
    for name, promql in PROM_QUERIES:
        facts["queries"][name] = instant_query(base, promql, log)
    return facts


# ---- Kubernetes (optional, read-only) ----------------------------------------


def _k8s_config(env: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Resolve k8s API access: explicit env first, then in-cluster SA."""
    url = (env.get("CFOP_DISCOVERY_K8S_URL") or "").strip()
    token = (env.get("CFOP_DISCOVERY_K8S_TOKEN") or "").strip()
    if url:
        return {"url": url.rstrip("/"), "token": token, "ca": None}
    host = env.get("KUBERNETES_SERVICE_HOST")
    if host and os.path.exists(f"{_SA_DIR}/token"):
        with open(f"{_SA_DIR}/token") as f:
            token = f.read().strip()
        port = env.get("KUBERNETES_SERVICE_PORT", "443")
        return {"url": f"https://{host}:{port}", "token": token, "ca": f"{_SA_DIR}/ca.crt"}
    return None


def _k8s_context(cfg: Dict[str, Any], env: Dict[str, str]) -> Optional[ssl.SSLContext]:
    if not cfg["url"].startswith("https"):
        return None
    if env.get("CFOP_DISCOVERY_K8S_INSECURE", "").lower() in ("1", "true", "yes"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # nosec - explicit operator opt-in for trials
        return ctx
    if cfg["ca"]:
        return ssl.create_default_context(cafile=cfg["ca"])
    return ssl.create_default_context()


def _summarize_nodes(payload: Any) -> List[Dict[str, Any]]:
    nodes = []
    for item in ((payload or {}).get("items") or [])[:MAX_SERIES_PER_QUERY]:
        meta = item.get("metadata", {}) or {}
        status = item.get("status", {}) or {}
        info = status.get("nodeInfo", {}) or {}
        labels = meta.get("labels", {}) or {}
        nodes.append({
            "name": meta.get("name"),
            "roles": sorted(k.rsplit("/", 1)[1] for k in labels if k.startswith("node-role.kubernetes.io/")),
            "arch": info.get("architecture"),
            "os_image": info.get("osImage"),
            "kubelet": info.get("kubeletVersion"),
            "capacity": {k: v for k, v in (status.get("capacity") or {}).items() if k in ("cpu", "memory")},
        })
    return nodes


def enumerate_kubernetes(env: Dict[str, str], log: QueryLog) -> Optional[Dict[str, Any]]:
    cfg = _k8s_config(env)
    if cfg is None:
        log.record("kubernetes", "no API access configured", "skipped")
        return None
    ctx = _k8s_context(cfg, env)
    headers = {"Authorization": f"Bearer {cfg['token']}"} if cfg["token"] else {}

    def get(path: str) -> Optional[Any]:
        return _get_json(f"{cfg['url']}{path}", log, "kubernetes", f"GET {path}",
                         headers=headers, context=ctx)

    nodes = get("/api/v1/nodes?limit=500")
    namespaces = get("/api/v1/namespaces?limit=500")
    deployments = get("/apis/apps/v1/deployments?limit=1000")
    daemonsets = get("/apis/apps/v1/daemonsets?limit=1000")

    def per_namespace(payload: Any) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for item in ((payload or {}).get("items") or []):
            ns = (item.get("metadata") or {}).get("namespace", "?")
            counts[ns] = counts.get(ns, 0) + 1
        return counts

    return {
        "nodes": _summarize_nodes(nodes),
        "namespaces": [(i.get("metadata") or {}).get("name")
                       for i in ((namespaces or {}).get("items") or [])[:MAX_SERIES_PER_QUERY]],
        "deployments_per_namespace": per_namespace(deployments),
        "daemonsets_per_namespace": per_namespace(daemonsets),
    }


# ---- entry -------------------------------------------------------------------


def enumerate_fleet(env: Dict[str, str], log: QueryLog) -> Dict[str, Any]:
    """The full deterministic pass. PROMETHEUS_URL is the one hard requirement."""
    prom_url = (env.get("PROMETHEUS_URL") or "").strip()
    if not prom_url:
        raise ValueError("PROMETHEUS_URL is required")
    facts: Dict[str, Any] = {"prometheus": enumerate_prometheus(prom_url, log)}
    k8s = enumerate_kubernetes(env, log)
    if k8s is not None:
        facts["kubernetes"] = k8s
    return facts
