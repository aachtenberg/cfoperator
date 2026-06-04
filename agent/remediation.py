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

import base64
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger("cfoperator.remediation")

WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet")


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
    taint_detail: str = ""        # 'key: value' that drives the toleration
    pr_title: str = ""
    pr_body: str = ""
    pr_result: Optional[Dict[str, Any]] = None   # set after a live open_pr()

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
        if self.pr_result:
            d["remediation_pr"] = self.pr_result
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
        repo=repo, patch_yaml=patch, taint_detail=reasons.get("taint_detail") or "",
        pr_title=title, pr_body=body,
    )


# --- manifest location + patching (live PR path) -----------------------------

_SECRET_PATH = re.compile(r"(sealed|secret|\.env|credential|token)", re.I)


def _is_secret_path(path: str) -> bool:
    """Refuse to patch anything that looks secret-bearing."""
    return bool(_SECRET_PATH.search(path or ""))


def _manifest_matches(text: str, workload: str, namespace: str, kinds=WORKLOAD_KINDS) -> bool:
    try:
        docs = list(yaml.safe_load_all(text))
    except Exception:
        return False
    for d in docs:
        if not isinstance(d, dict):
            continue
        if d.get("kind") not in kinds:
            continue
        meta = d.get("metadata") or {}
        if meta.get("name") != workload:
            continue
        ns = meta.get("namespace")
        if ns and namespace and ns != namespace:
            continue
        return True
    return False


def _find_pod_containers(lines: List[str]) -> tuple:
    """Locate the pod-spec ``containers:`` line; return (index, indent_str)."""
    cands = []
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s+)containers:\s*$", ln)
        if m:
            cands.append((len(m.group(1)), i, m.group(1)))
    if not cands:
        return (None, None)
    # Deepest indentation = the pod template's spec.containers (not a sibling map).
    cands.sort(reverse=True)
    _, idx, indent = cands[0]
    return (idx, indent)


def apply_toleration(text: str, taint_detail: Optional[str]) -> Optional[str]:
    """Return ``text`` with a toleration added as a sibling of the pod spec's
    ``containers:`` (preserving formatting/comments), or None if it can't be
    done safely. Declines when tolerations already exist, the structure isn't a
    workload, or the result wouldn't parse."""
    key = (taint_detail or "").split(":", 1)[0].strip()
    if not key:
        return None
    try:
        docs = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    except Exception:
        return None
    wd = next((d for d in docs if d.get("kind") in WORKLOAD_KINDS), None)
    if not wd:
        return None
    podspec = (((wd.get("spec") or {}).get("template") or {}).get("spec") or {})
    if not podspec.get("containers"):
        return None
    if podspec.get("tolerations"):
        return None  # already tolerates something — don't clobber, decline

    lines = text.splitlines()
    cidx, indent = _find_pod_containers(lines)
    if cidx is None:
        return None
    block = [
        f"{indent}tolerations:",
        f'{indent}  - key: "{key}"',
        f'{indent}    operator: "Exists"',
        f'{indent}    effect: "NoSchedule"',
    ]
    new_lines = lines[:cidx] + block + lines[cidx:]
    new_text = "\n".join(new_lines) + ("\n" if text.endswith("\n") else "")
    # Validate: parses, and the toleration actually landed on the pod spec.
    try:
        nd = [d for d in yaml.safe_load_all(new_text) if isinstance(d, dict)]
    except Exception:
        return None
    nw = next((d for d in nd if d.get("kind") in WORKLOAD_KINDS), {})
    tols = (((nw.get("spec") or {}).get("template") or {}).get("spec") or {}).get("tolerations")
    if not tols or not any(t.get("key") == key for t in tols if isinstance(t, dict)):
        return None
    return new_text


def locate_manifest(client: Any, repo: str, workload: str, namespace: str,
                    base_branch: str, max_candidates: int = 8) -> Optional[tuple]:
    """Find the manifest file that defines ``workload`` in ``repo`` via the
    GitHub API. Returns (path, text, file_sha) or None."""
    br = client.request("GET", f"/repos/{repo}/branches/{base_branch}")
    if not br.get("success"):
        return None
    tree_sha = ((((br.get("data") or {}).get("commit") or {}).get("commit") or {})
                .get("tree") or {}).get("sha")
    if not tree_sha:
        return None
    tr = client.request("GET", f"/repos/{repo}/git/trees/{tree_sha}", params={"recursive": "1"})
    if not tr.get("success"):
        return None
    paths = [t["path"] for t in (tr.get("data") or {}).get("tree", [])
             if t.get("type") == "blob" and re.search(r"\.ya?ml$", t.get("path", ""))]
    wl = workload.lower()
    ranked = sorted(paths, key=lambda p: (
        0 if wl in p.rsplit("/", 1)[-1].lower() else (1 if wl in p.lower() else 2)))
    for path in ranked[:max_candidates]:
        if wl not in path.lower():
            break  # ranked: once we leave name-matching paths, stop
        got = _get_file(client, repo, path, base_branch)
        if not got:
            continue
        text, sha = got
        if _manifest_matches(text, workload, namespace):
            return (path, text, sha)
    return None


def _get_file(client: Any, repo: str, path: str, ref: str) -> Optional[tuple]:
    r = client.request("GET", f"/repos/{repo}/contents/{path}", params={"ref": ref})
    if not r.get("success"):
        return None
    data = r.get("data") or {}
    if data.get("encoding") == "base64" and data.get("content"):
        text = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        return (text, data.get("sha"))
    return None


class RemediationProposer:
    """IO wrapper: fetch a pod's scheduling state, build a Proposal, and
    (only when ``open_prs`` is set) open a PR. Phase B keeps live PR opening
    deferred — ``open_prs`` defaults False and the live path is a guarded TODO.
    """

    def __init__(self, k8s_tools: Any, repos: Optional[List[Dict[str, Any]]] = None,
                 *, open_prs: bool = False, default_repo_name: str = "homelab-infra",
                 github: Any = None):
        self.k8s = k8s_tools
        self.repos = repos or []
        self.open_prs = bool(open_prs)
        self.default_repo_name = default_repo_name
        self.github = github  # a GitHubApiClient-like object with .request(); None => dry-run

    def _repo_slug(self, name: str) -> str:
        for r in self.repos:
            if r.get("name") == name:
                return r.get("github", "")
        return ""

    def _base_branch(self, name: str) -> str:
        for r in self.repos:
            if r.get("name") == name:
                return r.get("branch") or "main"
        return "main"

    @staticmethod
    def _branch_name(namespace: str, workload: str) -> str:
        slug = re.sub(r"[^a-z0-9-]+", "-", f"{namespace}-{workload}".lower()).strip("-")
        return f"cfop/remediate-{slug}"

    def open_pr(self, proposal: Proposal, namespace: str, workload: str) -> Optional[Dict[str, Any]]:
        """Open a real PR for a patch proposal. No-op (returns None) unless
        ``open_prs`` is set and a GitHub client is present. Idempotent: skips if
        the remediation branch already exists. Refuses secret-bearing files."""
        if not (self.open_prs and self.github and proposal.is_patch):
            return None
        repo = proposal.repo
        if not repo:
            return {"status": "error", "detail": "no repo configured for patch"}
        base = self._base_branch(self.default_repo_name)
        branch = self._branch_name(namespace, workload)

        # Dedupe: one open remediation branch per workload.
        existing = self.github.request("GET", f"/repos/{repo}/git/ref/heads/{branch}")
        if existing.get("success"):
            return {"status": "skipped", "detail": "remediation branch already exists", "branch": branch}

        located = locate_manifest(self.github, repo, workload, namespace, base)
        if not located:
            return {"status": "declined", "detail": "could not locate the owning manifest"}
        path, text, file_sha = located
        if _is_secret_path(path):
            return {"status": "refused", "detail": f"won't patch secret-bearing file: {path}"}

        patched = apply_toleration(text, proposal.taint_detail)
        if not patched or patched == text:
            return {"status": "declined", "detail": "could not generate a safe patch"}

        head = self.github.request("GET", f"/repos/{repo}/git/ref/heads/{base}")
        head_sha = ((head.get("data") or {}).get("object") or {}).get("sha") if head.get("success") else None
        if not head_sha:
            return {"status": "error", "detail": f"could not read base ref {base}"}

        cr = self.github.request("POST", f"/repos/{repo}/git/refs",
                                 body={"ref": f"refs/heads/{branch}", "sha": head_sha})
        if not cr.get("success"):
            return {"status": "error", "detail": f"branch create failed ({cr.get('status')})"}

        commit = self.github.request("PUT", f"/repos/{repo}/contents/{path}", body={
            "message": proposal.pr_title,
            "content": base64.b64encode(patched.encode("utf-8")).decode("ascii"),
            "branch": branch,
            "sha": file_sha,
        })
        if not commit.get("success"):
            return {"status": "error", "detail": f"commit failed ({commit.get('status')})"}

        pr = self.github.request("POST", f"/repos/{repo}/pulls", body={
            "title": proposal.pr_title, "body": proposal.pr_body, "head": branch, "base": base})
        if not pr.get("success"):
            return {"status": "error", "detail": f"PR create failed ({pr.get('status')})"}
        data = pr.get("data") or {}
        return {"status": "opened", "pr_number": data.get("number"),
                "html_url": data.get("html_url"), "branch": branch, "path": path}

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
