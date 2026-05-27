"""Stdlib-only notification sinks for Slack and Discord webhooks."""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
from typing import Dict

from .plugins import NotificationSink

logger = logging.getLogger(__name__)

# Actions whose completion is too noisy to notify about by default.
_DEFAULT_SKIP_ACTIONS = frozenset({"log_only"})


def _triage_attribution(details: Dict | None) -> str:
    """Return e.g. 'ollama/qwen3-coder:latest', or empty string.

    Used to mark which LLM actually executed the triage classification.
    Backend-only or model-only renderings degrade gracefully if only one
    field was populated (e.g. safe-default path on agent unreachable).
    """
    if not details:
        return ""
    params = details.get("decision_params") or {}
    backend = params.get("triage_backend") or ""
    model = params.get("triage_model") or ""
    if backend and model:
        return f"{backend}/{model}"
    return backend or model


def _format_message(summary: str, *, severity: str, details: Dict | None) -> str:
    """Build a plain-text notification body from an action result.

    For ``notify`` actions (the triage outcome that means "operator should
    see this but no LLM dive needed"), collapse to a single line. The whole
    point of triage routing to notify is to reduce Slack volume; emitting
    four lines of Action/Alert/Result here would defeat that.

    All other actions get the long form with Alert/Action/Result and any
    result_details whitelist keys.

    When the decision came from an LLM triage step, the LLM that served
    the classification is surfaced in both formats so operators can see
    which model triaged each alert (cost attribution + debugging when
    different LLMs disagree).
    """
    triage = _triage_attribution(details)

    if details and details.get("action") == "notify":
        alert_summary = details.get("alert_summary", "") or summary
        line = f"[{severity}] {alert_summary}"
        if triage:
            line += f"  ·  triaged by {triage}"
        return line

    parts = [summary]
    if details:
        action = details.get("action", "")
        alert_summary = details.get("alert_summary", "")
        result_message = details.get("result_message", "")
        if alert_summary:
            parts.append(f"Alert: {alert_summary}")
        if action:
            parts.append(f"Action: {action}")
        if result_message:
            parts.append(f"Result: {result_message}")
        if triage:
            parts.append(f"Triaged by: {triage}")
        # Surface key result details (e.g. PR URL, issue number, investigation link)
        result_details = details.get("result_details") or {}
        for key in ("html_url", "pr_number", "issue_number", "url", "investigation_url", "investigation_id"):
            if key in result_details:
                parts.append(f"{key}: {result_details[key]}")
    return "\n".join(parts)


def should_notify(
    action: str,
    success: bool,
    *,
    quiet: bool = False,
    skip_actions: frozenset[str] = _DEFAULT_SKIP_ACTIONS,
) -> bool:
    """Return whether this completed action warrants a notification.

    ``quiet`` lets a handler suppress its own notification (used for interim
    states like 'investigation queued' where the real completion notification
    comes from a separate post-back path).
    """
    if quiet:
        return False
    if action in skip_actions:
        return False
    return True


class SlackNotificationSink(NotificationSink):
    """Deliver notifications to a Slack incoming-webhook URL (stdlib only)."""

    name = "slack-notification"

    def __init__(self, webhook_url: str, *, timeout: int = 10):
        self.webhook_url = webhook_url
        self.timeout = timeout

    def notify(self, summary: str, *, severity: str = "info", details: Dict | None = None) -> bool:
        if not self.webhook_url:
            return False

        emoji = {
            "info": ":information_source:",
            "warning": ":warning:",
            "critical": ":rotating_light:",
        }.get(severity, ":robot_face:")

        text = _format_message(summary, severity=severity, details=details)
        payload = {"text": f"{emoji} *CFOperator Event Runtime*\n{text}"}

        return self._post(payload)

    def _post(self, payload: dict) -> bool:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError) as exc:
            logger.warning("Slack notification failed: %s", exc)
            return False


class DiscordNotificationSink(NotificationSink):
    """Deliver notifications to a Discord webhook URL (stdlib only)."""

    name = "discord-notification"

    def __init__(self, webhook_url: str, *, timeout: int = 10):
        self.webhook_url = webhook_url
        self.timeout = timeout

    def notify(self, summary: str, *, severity: str = "info", details: Dict | None = None) -> bool:
        if not self.webhook_url:
            return False

        color = {
            "info": 0x3498DB,
            "warning": 0xF39C12,
            "critical": 0xE74C3C,
        }.get(severity, 0x95A5A6)

        text = _format_message(summary, severity=severity, details=details)
        payload = {
            "embeds": [
                {
                    "title": f"CFOperator Event Runtime \u2014 {severity.upper()}",
                    "description": text[:4096],
                    "color": color,
                }
            ]
        }

        return self._post(payload)

    def _post(self, payload: dict) -> bool:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status in (200, 204)
        except (urllib.error.URLError, OSError) as exc:
            logger.warning("Discord notification failed: %s", exc)
            return False
