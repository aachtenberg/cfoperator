"""Portable remediation executor entrypoint.

Runs as an ephemeral Job spawned by the agent's drainer. Contract only — no
imports from the agent monolith:

  in   env: CFOP_REMEDIATION_JSON (work order), CFOP_COMPLETION_URL/_TOKEN,
            GITHUB_TOKEN, CFOP_GIT_REPO/_BASE, CFOP_TEMPLATES_DIR, + LLM env
  do   render remediation.md -> swappable LLM -> unified diff -> open PR
  out  POST the outcome to the agent callback, which drives the queue row

Read-only toward the cluster: the only mutation is a GitHub PR, which a human
merges and ArgoCD then syncs. Stdlib only.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from diff import extract_diff_block
from github import GitHubClient, open_pr_from_diff
from llm import make_llm

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("cfop-executor")


def load_work_order(env: Dict[str, str]) -> Dict[str, Any]:
    """Parse the CFOP_REMEDIATION_JSON env work order into a dict."""
    raw = (env.get("CFOP_REMEDIATION_JSON") or "").strip()
    if not raw:
        raise ValueError("CFOP_REMEDIATION_JSON is empty")
    order = json.loads(raw)
    if not isinstance(order, dict) or "id" not in order:
        raise ValueError("work order must be an object with an 'id'")
    return order


def build_prompt(template_text: str, work_order: Dict[str, Any]) -> str:
    """Fill the remediation template with the work order (brace-safe replace)."""
    payload = work_order.get("payload") or {}
    target = payload.get("target") or {}
    fields = {
        "{recommendation}": str(payload.get("recommendation", "")),
        "{remediation_class}": str(work_order.get("remediation_class", "")),
        "{context}": str(payload.get("rendered_context", "")),
        "{repo}": str(payload.get("repo", "")),
        "{target}": json.dumps(target),
    }
    out = template_text
    for key, value in fields.items():
        out = out.replace(key, value)
    return out


def classify_result(diff: Optional[str], pr_result: Optional[Dict[str, Any]]) -> Tuple[str, Optional[str], str]:
    """Map the executor outcome to a queue status.

    Returns (status, pr_url, detail). Raises on a hard PR error so the Job
    exits non-zero and the lease is reaped/retried; deterministic 'won't apply'
    cases route to needs-human instead.
    """
    if not diff:
        return "needs-human", None, "model produced no applicable diff"
    status = (pr_result or {}).get("status")
    if status in ("opened", "skipped"):
        return "pr-open", (pr_result or {}).get("html_url"), status
    if status in ("declined", "refused"):
        return "needs-human", None, str((pr_result or {}).get("detail", status))
    raise RuntimeError(f"PR open failed: {(pr_result or {}).get('detail', status)}")


def build_completion_payload(work_order: Dict[str, Any], status: str,
                             pr_url: Optional[str], detail: str,
                             pr_result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "remediation_id": work_order["id"],
        "status": status,
        "pr_url": pr_url,
        "detail": detail,
        "result": pr_result or {},
    }


def post_completion(url: str, payload: Dict[str, Any], *, token: str = "", retries: int = 3) -> bool:
    body = json.dumps(payload, default=str).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-CFOP-Token"] = token
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:  # nosec
                if 200 <= resp.status < 300:
                    return True
        except Exception as e:  # noqa: BLE001 - completion is best-effort with retries
            logger.warning("completion POST attempt %d failed: %s", attempt, e)
    return False


def run(env: Dict[str, str]) -> Dict[str, Any]:
    """Execute one remediation work order; returns the completion payload."""
    work_order = load_work_order(env)
    payload = work_order.get("payload") or {}

    templates_dir = Path(env.get("CFOP_TEMPLATES_DIR", str(Path.home() / "templates")))
    template_text = (templates_dir / "remediation.md").read_text(encoding="utf-8")
    prompt = build_prompt(template_text, work_order)

    llm = make_llm(env)
    report = llm.complete(prompt)
    diff = extract_diff_block(report)

    pr_result: Optional[Dict[str, Any]] = None
    if diff:
        token = (env.get("GITHUB_TOKEN") or "").strip()
        if not token:
            raise RuntimeError("GITHUB_TOKEN unset; cannot open remediation PR")
        client = GitHubClient(token)
        repo = (env.get("CFOP_GIT_REPO") or payload.get("repo") or "").strip()
        base = (env.get("CFOP_GIT_BASE") or "main").strip()
        if not repo:
            raise RuntimeError("no target repo configured (CFOP_GIT_REPO)")
        pr_result = open_pr_from_diff(
            client, repo=repo, base=base, diff_text=diff,
            title=f"cfop: remediate {payload.get('recommendation', 'issue')[:60]}",
            body=_pr_body(work_order, report),
            dedupe_key=f"{work_order.get('investigation_id') or work_order['id']}",
        )

    status, pr_url, detail = classify_result(diff, pr_result)
    return build_completion_payload(work_order, status, pr_url, detail, pr_result)


def _pr_body(work_order: Dict[str, Any], report: str) -> str:
    payload = work_order.get("payload") or {}
    return (
        f"Automated remediation proposed by cfoperator-executor.\n\n"
        f"**Recommendation:** {payload.get('recommendation', 'n/a')}\n"
        f"**Class:** {work_order.get('remediation_class')}  "
        f"**Risk:** {work_order.get('risk')}  "
        f"**Confidence:** {work_order.get('confidence')}\n"
        f"**Source investigation:** {work_order.get('investigation_id')}\n\n"
        f"---\n_Human merge is the only path to apply this; ArgoCD syncs on merge._\n\n"
        f"<details><summary>Executor report</summary>\n\n{report[:6000]}\n\n</details>"
    )


def main() -> int:
    env = dict(os.environ)
    completion_url = (env.get("CFOP_COMPLETION_URL") or "").strip()
    token = (env.get("CFOP_COMPLETION_TOKEN") or "").strip()
    try:
        payload = run(env)
    except Exception as e:  # noqa: BLE001 - any failure must report, not vanish
        logger.error("executor failed: %s", e, exc_info=True)
        order_id = None
        try:
            order_id = load_work_order(env).get("id")
        except Exception:
            pass
        payload = {"remediation_id": order_id, "status": "failed", "pr_url": None,
                   "detail": str(e), "result": {}}
        if completion_url:
            post_completion(completion_url, payload, token=token)
        return 1
    logger.info("executor outcome: %s (%s)", payload["status"], payload.get("detail"))
    if completion_url and not post_completion(completion_url, payload, token=token):
        logger.error("failed to post completion; agent reaper will recover the lease")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
