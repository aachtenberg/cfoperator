"""Deep-investigation worker entrypoint.

Runs inside the cfoperator-worker Job pod (see worker/Dockerfile). Renders
the prompt template selected by the launching handler, runs headless Claude
Code with a read-only tool allowlist, and posts the report back to the event
runtime's completion endpoint — the same protocol the agent's investigate
loop uses, so notifications/audit/escalation-ledger behavior is identical.

Stdlib only (urllib, subprocess, json): testable with the repo's
``patch("urllib.request.urlopen")`` convention and no extra image deps.

Failure containment: ``main()`` posts a ``success=False`` completion on ANY
exception, so the alert never silently stalls; the only uncovered case is a
hard pod kill (OOM / activeDeadlineSeconds), which the Job's 6h TTL leaves
visible for the operator.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("cfop-worker")

_REPORT_MAX_CHARS = 20_000
_VALID_STATUSES = ("resolved", "needs_action", "monitoring", "escalate")
# STATUS: escalate in the report maps to outcome "escalated" — that exact
# string is what engine.record_external_action_completion uses to mark the
# escalation ledger for later resolution notifications.
_STATUS_TO_OUTCOME = {
    "resolved": "resolved",
    "needs_action": "needs_action",
    "monitoring": "monitoring",
    "escalate": "escalated",
}

# Remediation classification — how the recommendation could be applied, so the
# remediation drainer can route and risk-gate it. These are diagnostic
# (read-only) classifications, not actions; the worker stays strictly read-only.
#   gitops-patch: a manifest change to homelab-infra (paired with a ```diff block)
#   k8s-action:   an in-cluster change expressible as a MANIFEST EDIT
#                 (scale replicas, rollout restart via annotation)
#   k8s-imperative: a one-off kubectl verb with no manifest equivalent
#                 (create Job from CronJob, delete pod, cordon) — parks for a human
#   node-action:  a node-state change (DNS, files, systemd) via ssh/ansible
#   data-fix:     a database-row change; parks for a human (nothing executes it)
#   external-system: a change in a system we do not operate; parks for a human
#   manual:       needs human judgement; not safely mechanizable
_VALID_REMEDIATION_CLASSES = ("gitops-patch", "k8s-action", "k8s-imperative",
                              "node-action", "data-fix", "external-system",
                              "manual")
_VALID_RISKS = ("low", "med", "high")

# Read-only tool allowlist. --permission-mode dontAsk auto-denies anything
# not listed, so a prompt-injected `kubectl delete` simply fails. The space
# before * is required prefix-match syntax.
_ALLOWED_TOOLS = (
    "Bash(ssh *)",
    "Bash(kubectl get *)",
    "Bash(kubectl describe *)",
    "Bash(kubectl top *)",
    "Read",
)


@dataclass
class WorkerInputs:
    alert: Dict[str, Any]
    deep_context: Dict[str, Any]
    template: str
    target_host: str
    ssh_user: str
    model: str
    claude_timeout: int
    completion_url: str
    completion_token: str
    agent_url: str
    templates_dir: Path
    ssh_secret_dir: Path


def load_inputs(env: Dict[str, str] | None = None) -> WorkerInputs:
    env = env if env is not None else dict(os.environ)

    def _json_env(name: str) -> Dict[str, Any]:
        raw = env.get(name, "").strip()
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except ValueError:
            logger.warning("Unparseable %s; continuing without it", name)
            return {}
        return value if isinstance(value, dict) else {}

    return WorkerInputs(
        alert=_json_env("CFOP_ALERT_JSON"),
        deep_context=_json_env("CFOP_DEEP_CONTEXT_JSON"),
        template=env.get("CFOP_TEMPLATE", "host-forensics").strip(),
        target_host=env.get("CFOP_TARGET_HOST", "").strip(),
        # No default (CFOP-137). This worker does NOT ssh itself -- it renders
        # "ssh {ssh_user}@{host}" into the Claude prompt -- so an empty value is
        # not a connect-time error, it is a BILLED forensics Job that tries
        # "@host" until the deadline. Validated below rather than defaulted;
        # the old fallback was a personal username in a public image.
        ssh_user=env.get("CFOP_SSH_USER", "").strip(),
        model=env.get("CLAUDE_MODEL", "").strip(),
        claude_timeout=int(env.get("CFOP_CLAUDE_TIMEOUT", "600") or 600),
        completion_url=env.get("CFOP_COMPLETION_URL", "").strip(),
        completion_token=env.get("CFOP_COMPLETION_TOKEN", "").strip(),
        agent_url=env.get("CFOP_AGENT_URL", "").strip(),
        templates_dir=Path(env.get("CFOP_TEMPLATES_DIR", str(Path.home() / "templates"))),
        ssh_secret_dir=Path(env.get("CFOP_SSH_SECRET_DIR", "/ssh-secret")),
    )


def prepare_ssh(secret_dir: Path, ssh_dir: Path) -> None:
    """Copy the mounted SSH secret into ~/.ssh with key-safe permissions.

    Secret volumes are root-owned and (at best) group-readable; the ssh
    client refuses group-readable private keys, so a direct mount at ~/.ssh
    doesn't work. Missing secret dir is not fatal here — the investigation
    fails later with a clearer "ssh failed" in the report.
    """
    if not secret_dir.is_dir():
        logger.warning("SSH secret dir %s missing; ssh will be unavailable", secret_dir)
        return
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    for entry in secret_dir.iterdir():
        # Secret volumes use ..data/ symlink indirection; iterdir on the
        # mount root yields the projected files plus dot-dirs — skip those.
        if entry.name.startswith(".") or not entry.is_file():
            continue
        target = ssh_dir / entry.name
        shutil.copyfile(entry, target)
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600


def build_prompt(template_text: str, inputs: WorkerInputs) -> str:
    """Render the template with simple placeholder substitution.

    Deliberately not str.format(): report templates legitimately contain
    braces (fenced diff blocks, JSON examples) that format() would choke on.
    """
    alert = inputs.alert
    prior = inputs.deep_context
    prior_text = (
        json.dumps(prior, indent=2, default=str) if prior else "(none — routed directly)"
    )
    substitutions = {
        "{host}": inputs.target_host,
        "{ssh_user}": inputs.ssh_user,
        "{alert_summary}": str(alert.get("summary", "(no summary)")),
        "{severity}": str(alert.get("severity", "unknown")),
        "{occurred_at}": str(alert.get("occurred_at", "unknown")),
        "{prior_findings}": prior_text,
    }
    rendered = template_text
    for placeholder, value in substitutions.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


@dataclass
class ClaudeRun:
    success: bool
    report: str
    error: str
    duration_s: float
    cost_usd: Optional[float] = None
    retryable: bool = False


# API failures the CLI reports via is_error/api_error_status that are worth
# one retry: rate limit, transient server errors, overloaded.
_RETRYABLE_API_STATUSES = {429, 500, 502, 503, 529}


def run_claude(prompt: str, *, model: str, timeout: int, cwd: str | None = None) -> ClaudeRun:
    """Run headless Claude Code and return its final report text.

    On failure the CLI exits nonzero with an EMPTY stderr — the error detail
    lives in the stdout JSON (``is_error``/``api_error_status``/``result``),
    so that is what gets surfaced. Transient API errors get one retry.
    """
    run = _run_claude_once(prompt, model=model, timeout=timeout, cwd=cwd)
    if not run.success and run.retryable:
        logger.warning("claude failed retryably (%s); retrying once in 15s", run.error)
        time.sleep(15)
        retry = _run_claude_once(prompt, model=model, timeout=timeout, cwd=cwd)
        retry.duration_s += run.duration_s + 15
        return retry
    return run


def _run_claude_once(prompt: str, *, model: str, timeout: int, cwd: str | None = None) -> ClaudeRun:
    cmd = [
        "claude",
        "-p", prompt,
        "--output-format", "json",
        "--max-turns", "30",
        "--permission-mode", "dontAsk",
        "--allowedTools", *_ALLOWED_TOOLS,
    ]
    if model:
        cmd.extend(["--model", model])
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or str(Path.home()),
        )
    except subprocess.TimeoutExpired:
        return ClaudeRun(False, "", f"claude timed out after {timeout}s", time.monotonic() - started)
    except FileNotFoundError:
        return ClaudeRun(False, "", "claude CLI not found on PATH", time.monotonic() - started)
    duration = time.monotonic() - started

    payload: Dict[str, Any] = {}
    try:
        parsed = json.loads(proc.stdout)
        if isinstance(parsed, dict):
            payload = parsed
    except ValueError:
        pass

    api_status = payload.get("api_error_status")
    if proc.returncode != 0 or payload.get("is_error"):
        detail = str(payload.get("result") or "").strip() or (proc.stderr or "").strip()[:500] \
            or (proc.stdout or "").strip()[:500] or "(no output)"
        error = f"claude exited {proc.returncode}"
        if api_status:
            error += f" (api_error_status={api_status})"
        error += f": {detail[:300]}"
        run = ClaudeRun(False, "", error, duration)
        run.retryable = isinstance(api_status, int) and api_status in _RETRYABLE_API_STATUSES
        return run

    report = str(payload.get("result") or "") if payload else proc.stdout.strip()
    cost = payload.get("total_cost_usd")
    cost_usd = float(cost) if isinstance(cost, (int, float)) else None
    if not report:
        return ClaudeRun(False, "", "claude returned an empty report", duration)
    return ClaudeRun(True, report, "", duration, cost_usd)


def parse_status(report: str) -> str:
    """Extract the trailing STATUS: line; default to needs_action.

    needs_action is the safe default — an unparseable report still pages the
    operator instead of being filed as resolved/quiet.
    """
    for line in reversed(report.strip().splitlines()):
        match = re.match(r"^\s*STATUS:\s*(\w+)", line)
        if match and match.group(1).lower() in _VALID_STATUSES:
            return match.group(1).lower()
    return "needs_action"


def parse_recommendation(report: str) -> str:
    for line in reversed(report.strip().splitlines()):
        match = re.match(r"^\s*RECOMMENDATION:\s*(.+)$", line)
        if match:
            return match.group(1).strip()
    return ""


def parse_remediation_class(report: str) -> str:
    """Extract the trailing REMEDIATION_CLASS: line; "" if absent/invalid.

    Empty means the investigator did not classify the recommendation — the
    drainer treats that as manual (human-only), never auto-executed.
    """
    for line in reversed(report.strip().splitlines()):
        match = re.match(r"^\s*REMEDIATION_CLASS:\s*([\w-]+)", line)
        if match and match.group(1).lower() in _VALID_REMEDIATION_CLASSES:
            return match.group(1).lower()
    return ""


def parse_risk(report: str) -> str:
    """Extract the trailing RISK: line; "" if absent/invalid."""
    for line in reversed(report.strip().splitlines()):
        match = re.match(r"^\s*RISK:\s*(\w+)", line)
        if match and match.group(1).lower() in _VALID_RISKS:
            return match.group(1).lower()
    return ""


def parse_confidence(report: str) -> Optional[float]:
    """Extract the trailing CONFIDENCE: line as a 0.0-1.0 float; None if absent.

    Out-of-range values are clamped so a stray "CONFIDENCE: 95" reads as 1.0
    rather than poisoning the auto-execute gate.
    """
    for line in reversed(report.strip().splitlines()):
        match = re.match(r"^\s*CONFIDENCE:\s*(-?[0-9]*\.?[0-9]+)", line)
        if match:
            try:
                value = float(match.group(1))
            except ValueError:
                return None
            return max(0.0, min(1.0, value))
    return None


def extract_proposed_diff(report: str) -> Optional[str]:
    """Return the first ```diff fenced block, if any."""
    match = re.search(r"```diff\n(.*?)```", report, re.DOTALL)
    return match.group(1).strip() if match else None


def build_action_result(run: ClaudeRun, inputs: WorkerInputs) -> Dict[str, Any]:
    """Build the ActionResult-shaped completion payload (models.ActionResult)."""
    summary = str(inputs.alert.get("summary", ""))
    if not run.success:
        return {
            "action": "deep_investigate",
            "success": False,
            "message": f"Deep investigation failed ({run.error}) for: {summary}",
            "quiet": False,
            "details": {
                "outcome": "failed",
                "error": run.error,
                "host": inputs.target_host,
                "template": inputs.template,
                "model": inputs.model,
                "duration_s": round(run.duration_s, 1),
            },
        }

    status = parse_status(run.report)
    outcome = _STATUS_TO_OUTCOME[status]
    recommendation = parse_recommendation(run.report)
    diff = extract_proposed_diff(run.report)
    remediation_class = parse_remediation_class(run.report)
    risk = parse_risk(run.report)
    confidence = parse_confidence(run.report)
    details: Dict[str, Any] = {
        "outcome": outcome,
        "report": run.report[:_REPORT_MAX_CHARS],
        "host": inputs.target_host,
        "template": inputs.template,
        "model": inputs.model,
        "duration_s": round(run.duration_s, 1),
    }
    if recommendation:
        details["recommendation"] = recommendation  # hoisted into the notification
    if diff:
        details["proposed_diff"] = diff
    # Remediation routing hints for the queue/drainer (purely additive — absent
    # when the investigator omits the lines, in which case it stays manual).
    if remediation_class:
        details["remediation_class"] = remediation_class
    if risk:
        details["risk"] = risk
    if confidence is not None:
        details["confidence"] = confidence
    if run.cost_usd is not None:
        details["cost_usd"] = run.cost_usd
    return {
        "action": "deep_investigate",
        "success": True,
        "message": f"Deep investigation ({status}) of {inputs.target_host} for: {summary}",
        "quiet": False,
        "details": details,
    }


def post_json(url: str, payload: Dict[str, Any], *, token: str = "", retries: int = 3) -> bool:
    body = json.dumps(payload, default=str).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-CFOP-Token"] = token
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                response.read()
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            logger.warning("POST %s failed (attempt %d/%d): %s", url, attempt, retries, exc)
            if attempt < retries:
                time.sleep(2 ** attempt)
    return False


def post_completion(inputs: WorkerInputs, result: Dict[str, Any]) -> bool:
    if not inputs.completion_url:
        logger.error("No CFOP_COMPLETION_URL configured; report not delivered")
        return False
    payload = {"alert": inputs.alert, "result": result}
    return post_json(inputs.completion_url, payload, token=inputs.completion_token)


def post_kb_ingest(inputs: WorkerInputs, result: Dict[str, Any]) -> None:
    """Best-effort: hand the report to the agent so it lands in the KB with
    embeddings and (optionally) flows through the remediation PR gates."""
    if not inputs.agent_url:
        return
    url = f"{inputs.agent_url.rstrip('/')}/v1/deep-investigations"
    payload = {"alert": inputs.alert, "result": result}
    if not post_json(url, payload, token=inputs.completion_token, retries=2):
        logger.warning("KB ingest post failed (non-fatal)")


def main() -> int:
    inputs = load_inputs()
    logger.info(
        "Deep investigation starting: host=%s template=%s model=%s",
        inputs.target_host, inputs.template, inputs.model or "(default)",
    )
    # CFOP-137: refuse BEFORE spending a claude run. This worker renders
    # "ssh {ssh_user}@{host}" into the prompt rather than connecting itself, so
    # an empty user is not a fast connect error -- it is a BILLED Job that
    # flails at "@host" until claude_timeout. The spawner always injects this,
    # so arriving without it means the deploy never configured one. Posts back
    # like any other failure so the row does not sit waiting on a Job that
    # already gave up.
    if not inputs.ssh_user:
        msg = ("no SSH user configured: set event_runtime.deep_investigation."
               "ssh_user (or CFOP_DEEP_SSH_USER on the event runtime), which "
               "reaches this Job as CFOP_SSH_USER")
        logger.error(msg)
        result = build_action_result(ClaudeRun(False, "", msg, 0.0), inputs)
        return 0 if post_completion(inputs, result) else 1

    try:
        prepare_ssh(inputs.ssh_secret_dir, Path.home() / ".ssh")
        template_path = inputs.templates_dir / f"{inputs.template}.md"
        template_text = template_path.read_text(encoding="utf-8")
        prompt = build_prompt(template_text, inputs)
        run = run_claude(prompt, model=inputs.model, timeout=inputs.claude_timeout)
    except Exception as exc:  # noqa: BLE001 - any failure must still post back
        logger.exception("Worker crashed before/while running claude")
        run = ClaudeRun(False, "", f"worker error: {type(exc).__name__}: {exc}", 0.0)

    result = build_action_result(run, inputs)
    delivered = post_completion(inputs, result)
    if run.success:
        post_kb_ingest(inputs, result)
    logger.info(
        "Deep investigation finished: success=%s outcome=%s delivered=%s",
        run.success, result["details"].get("outcome"), delivered,
    )
    # Exit 0 whenever the completion was delivered: backoffLimit=0 means a
    # nonzero exit just marks the Job failed without retrying, and the
    # operator-facing failure signal already travelled through the post-back.
    return 0 if delivered else 1


if __name__ == "__main__":
    sys.exit(main())
