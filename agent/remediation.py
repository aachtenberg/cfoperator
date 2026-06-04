"""Remediation proposer (Phase B).

Turns a verified ``needs_action`` investigation about an *unschedulable pod*
into one of:

  - a concrete, reviewable **patch proposal** (e.g. add a toleration), or
  - a precise **decline** with the reason it isn't safe to auto-propose.

Design notes / safety:
  - **Dry-run by default.** ``build_proposal`` is pure and never touches the
    cluster or GitHub. The IO layer (``RemediationProposer``) produces a
    proposal; opening a real PR is gated behind ``open_prs=True`` and is a
    deliberate, separate step.
  - **Conservative.** The adguardhome incident (2026-06-04) showed the naive
    fix — "add a toleration" — can be actively wrong: that pod was pinned
    (nodeSelector) + hostNetwork, and its unschedulability was a host-port
    conflict, not a manifest defect. A toleration would have shoved a DNS
    server onto a tainted GPU node. So: hostNetwork/hostPort + "free ports"
    → decline; pinned pod whose node is full → decline; only a *non-pinned*
    pod blocked *solely* by an untolerated taint is proposed — and even then
    it goes out as a dry-run / human-reviewed PR, never auto-applied.

See docs/remediation-pipeline.md for the full design.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Proposal:
    """The outcome of considering a remediation for one finding.

    ``kind`` is 'patch' (we have a concrete change to propose) or 'decline'
    (we explain why we won't). ``reason`` is operator-facing and is suitable
    to surface as the investigation's recommendation.
    """

    kind: str  # 'patch' | 'decline'
    reason: str
    confidence: float = 0.0
    # patch-only fields
    fix_class: str = ""           # e.g. 'add_toleration'
    repo: str = ""                # github slug, e.g. 'aachtenberg/homelab-infra'
    patch_yaml: str = ""          # the snippet we propose to add
    pr_title: str = ""
    pr_body: str = ""

    @property
    def is_patch(self) -> bool:
        return self.kind == "patch"

    def to_details(self) -> Dict[str, Any]:
        """Compact dict for the ActionResult details / notification."""
        d = {"remediation_kind": self.kind, "remediation_reason": self.reason}
        if self.is_patch:
            d.update({
                "remediation_fix_class": self.fix_class,
                "remediation_repo": self.repo,
                "remediation_pr_title": self.pr_title,
            })
        return d


def parse_unschedulable(message: str) -> Dict[str, Any]:
    """Pull the structured blockers out of a FailedScheduling message.

    The scheduler emits lines like:
      "0/7 nodes are available: 1 node(s) didn't have free ports for the
       requested pod ports, 1 node(s) had untolerated taint {gpu: true},
       5 node(s) didn't match Pod's node affinity/selector."
    """
    msg = message or ""
    taint = re.search(r"untolerated taint \{([^}]*)\}", msg)
    return {
        "untolerated_taint": bool(re.search(r"untolerated taint", msg, re.I)),
        "taint_detail": taint.group(1).strip() if taint else None,
        "free_ports": bool(re.search(r"free ports", msg, re.I)),
        "affinity_mismatch": bool(re.search(r"node affinity/selector", msg, re.I)),
        "insufficient": sorted(set(re.findall(r"Insufficient (\w+)", msg))),
    }


def _has_host_ports(pod_spec: Dict[str, Any]) -> bool:
    for c in (pod_spec or {}).get("containers", []):
        for p in c.get("ports", []) or []:
            if p.get("hostPort"):
                return True
    return False


def classify(pod_spec: Dict[str, Any], reasons: Dict[str, Any]) -> tuple:
    """Decide what to do about an unschedulable pod. Returns (decision, reason).

    Decisions: 'propose_toleration' | 'decline_port_conflict' |
    'decline_pinned_full' | 'decline_insufficient' | 'decline_unknown'.
    """
    spec = pod_spec or {}
    host_network = bool(spec.get("hostNetwork"))
    pinned = bool(spec.get("nodeSelector")) or bool(spec.get("nodeName"))

    # 1) Host-port contention on a hostNetwork/hostPort pod is a placement/port
    #    conflict, never a manifest defect we should auto-patch. (The adguard case.)
    if reasons.get("free_ports") and (host_network or _has_host_ports(spec)):
        return ("decline_port_conflict",
                "unschedulable is a host-port conflict on a hostNetwork/hostPort pod — "
                "a node/port contention issue, not a manifest defect. Free the port on the "
                "target node (move or re-port the conflicting workload) rather than patching this pod.")

    # 2) Pinned to a node that can't fit it — tolerations won't help.
    if pinned and not reasons.get("untolerated_taint"):
        return ("decline_pinned_full",
                "pod is pinned via nodeSelector/nodeName and its target node can't fit it; "
                "a toleration won't help — needs capacity freed or the placement changed.")

    # 3) Insufficient cpu/memory — a requests/limits change, out of Phase B scope.
    if reasons.get("insufficient"):
        return ("decline_insufficient",
                f"blocked by insufficient {', '.join(reasons['insufficient'])}; needs a resource "
                "request/limit or node-capacity change (not yet automated).")

    # 4) The one case we'll propose: a *non-pinned* pod blocked *solely* by an
    #    untolerated taint. Even so it ships as a dry-run / human-reviewed PR.
    if reasons.get("untolerated_taint") and not pinned and not reasons.get("free_ports"):
        return ("propose_toleration",
                f"pod is unschedulable only because of an untolerated taint "
                f"({reasons.get('taint_detail') or 'unknown'}); propose adding a matching toleration.")

    return ("decline_unknown",
            "unschedulable cause isn't a pattern Phase B can safely auto-propose; left for a human.")


def render_toleration_patch(taint_detail: Optional[str]) -> str:
    """Render the tolerations snippet for a 'key: value' taint (Exists match)."""
    key = (taint_detail or "").split(":", 1)[0].strip() or "<taint-key>"
    return (
        "tolerations:\n"
        f"  - key: \"{key}\"\n"
        "    operator: \"Exists\"\n"
        "    effect: \"NoSchedule\"\n"
    )


def build_proposal(
    *,
    namespace: str,
    pod_name: str,
    workload: str,
    pod_spec: Dict[str, Any],
    scheduler_message: str,
    repo: str,
) -> Proposal:
    """Pure: turn a pod's spec + scheduler message into a Proposal. No IO."""
    reasons = parse_unschedulable(scheduler_message)
    decision, reason = classify(pod_spec, reasons)

    if decision != "propose_toleration":
        # Map decline confidence: port-conflict is a confident decline.
        conf = 0.9 if decision == "decline_port_conflict" else 0.6
        return Proposal(kind="decline", reason=reason, confidence=conf, fix_class=decision)

    patch = render_toleration_patch(reasons.get("taint_detail"))
    title = f"[cfoperator] tolerate taint for {namespace}/{workload}"
    body = (
        f"Automated remediation proposal (dry-run; review before merge).\n\n"
        f"**Finding:** pod `{namespace}/{pod_name}` is unschedulable.\n"
        f"**Scheduler:** {scheduler_message.strip()}\n\n"
        f"**Proposed change:** add a toleration to `{workload}` so it can schedule on the "
        f"tainted node:\n\n```yaml\n{patch}```\n"
        f"**Why this and not something else:** the pod is not pinned to another node and the "
        f"*only* blocker is the untolerated taint. Verify the taint is one this workload should "
        f"tolerate before merging — if the taint exists to keep general workloads off that node "
        f"(e.g. a GPU node), close this instead.\n"
    )
    return Proposal(
        kind="patch", reason=reason, confidence=0.7, fix_class="add_toleration",
        repo=repo, patch_yaml=patch, pr_title=title, pr_body=body,
    )


class RemediationProposer:
    """IO wrapper: fetch a pod's scheduling state, build a Proposal, and
    (only when ``open_prs`` is set) open a PR. Phase B keeps live PR opening
    deferred — ``open_prs`` defaults False and the live path is a guarded TODO.
    """

    def __init__(self, k8s_tools: Any, repos: Optional[List[Dict[str, Any]]] = None,
                 *, open_prs: bool = False, default_repo_name: str = "homelab-infra"):
        self.k8s = k8s_tools
        self.repos = repos or []
        self.open_prs = bool(open_prs)
        self.default_repo_name = default_repo_name

    def _repo_slug(self, name: str) -> str:
        for r in self.repos:
            if r.get("name") == name:
                return r.get("github", "")
        return ""

    def _scheduler_message(self, namespace: str, pod_name: str) -> str:
        """Best-effort FailedScheduling message from the pod's events."""
        if not self.k8s:
            return ""
        try:
            ev = self.k8s.get_events(namespace=namespace)
        except Exception:
            return ""
        items = ev.get("events") or ev.get("items") or []
        msgs = []
        for e in items:
            if not isinstance(e, dict):
                if "nodes are available" in str(e):
                    msgs.append(str(e))
                continue
            reason = e.get("reason", "")
            msg = e.get("message", "")
            if reason != "FailedScheduling" and "nodes are available" not in msg:
                continue
            # Events are namespace-scoped already; only skip when an explicit
            # involvedObject names a *different* pod.
            involved = (e.get("involvedObject") or {}).get("name", "") or e.get("name", "")
            if pod_name and involved and involved != pod_name:
                continue
            msgs.append(msg or str(e))
        return msgs[-1] if msgs else ""

    def propose_for(self, namespace: str, pod_name: str, workload: str = "") -> Optional[Proposal]:
        """Fetch live state and build a Proposal. Returns None when it can't
        gather enough to decide (caller treats that as 'no proposal')."""
        if not self.k8s:
            return None
        try:
            status = self.k8s.get_pod_status(namespace, pod_name)
        except Exception:
            return None
        if not status.get("success"):
            return None
        # Only act on genuinely-Pending pods.
        if status.get("phase") != "Pending":
            return None
        message = self._scheduler_message(namespace, pod_name)
        if not message:
            return None
        spec = self._fetch_pod_spec(namespace, pod_name)
        repo = self._repo_slug(self.default_repo_name)
        return build_proposal(
            namespace=namespace, pod_name=pod_name,
            workload=workload or pod_name, pod_spec=spec,
            scheduler_message=message, repo=repo,
        )

    def _fetch_pod_spec(self, namespace: str, pod_name: str) -> Dict[str, Any]:
        """Pull the scheduling-relevant slice of the pod spec via describe/get."""
        try:
            res = self.k8s.describe("pod", pod_name, namespace=namespace)
        except Exception:
            return {}
        # describe() returns raw text in this codebase; the pure classifier only
        # needs a few booleans, so derive them cheaply from the text.
        text = res.get("output", "") if isinstance(res, dict) else str(res)
        spec: Dict[str, Any] = {}
        if re.search(r"host network:\s*true", text, re.I) or re.search(r"hostNetwork:\s*true", text, re.I):
            spec["hostNetwork"] = True
        if re.search(r"Node-Selectors:\s*\S", text) and not re.search(r"Node-Selectors:\s*<none>", text, re.I):
            spec["nodeSelector"] = {"_present": "true"}
        return spec
