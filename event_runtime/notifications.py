"""Stdlib-only notification sinks for Slack, Discord, and ntfy webhooks."""

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


def _served_attribution(details: Dict | None) -> str:
    """Return the LLM that actually executed the action's work, e.g.
    'ollama/qwen3-coder:latest', or empty string.

    Distinct from ``_triage_attribution``: triage is the cheap classifier
    that *routed* the alert, while this is the model that did the heavy
    lifting (e.g. the multi-tool investigation loop). The agent records it
    as ``result_details['provider']`` (see agent._build_action_result), and
    it survives the external completion post-back where the triage Decision
    is no longer in scope — so it's often the only attribution available.
    """
    if not details:
        return ""
    result_details = details.get("result_details") or {}
    return str(result_details.get("provider") or "").strip()


def _format_message(summary: str, *, severity: str, details: Dict | None) -> str:
    """Build a plain-text notification body from an action result.

    For ``notify`` actions (the triage outcome that means "operator should
    see this but no LLM dive needed"), collapse to a single line. The whole
    point of triage routing to notify is to reduce Slack volume; emitting
    four lines of Action/Alert/Result here would defeat that.

    All other actions get the long form with Alert/Action/Result and any
    result_details whitelist keys.

    Two layers of LLM attribution are surfaced in both formats so operators
    can see which model did what (cost attribution + debugging when
    different LLMs disagree):
      - ``Investigated by:`` — the model that ran the action's work (the
        multi-tool investigation loop), read from
        ``result_details['provider']``. Survives the external completion
        post-back, so it's usually present even when triage attribution is
        not.
      - ``Triaged by:`` — the cheap classifier that routed the alert, read
        from the Decision's ``decision_params``.

    Two extra fields the engine can hoist out of ``Alert.details`` change
    the rendering shape:
      - ``recommendation``: operator-facing next step (rendered as a
        ``Recommendation:`` line, or ``recommend: …`` inline for notify).
      - ``resolution``: when truthy, the alert announces that a previously
        reported finding is now clear — replaces the ``[severity]`` prefix
        with ``:white_check_mark: Resolved:``.
    """
    triage = _triage_attribution(details)
    served = _served_attribution(details)
    recommendation = ""
    resolution = False
    if details:
        recommendation = str(details.get("recommendation") or "").strip()
        resolution = bool(details.get("resolution"))

    if details and details.get("action") == "notify":
        alert_summary = details.get("alert_summary", "") or summary
        if resolution:
            line = f":white_check_mark: Resolved: {alert_summary}"
        else:
            line = f"[{severity}] {alert_summary}"
        if recommendation:
            line += f"  ·  recommend: {recommendation}"
        if served:
            line += f"  ·  investigated by {served}"
        if triage:
            line += f"  ·  triaged by {triage}"
        return line

    parts = [summary]
    if details:
        action = details.get("action", "")
        alert_summary = details.get("alert_summary", "")
        result_message = details.get("result_message", "")
        if alert_summary:
            label = "Resolved" if resolution else "Alert"
            parts.append(f"{label}: {alert_summary}")
        if action:
            parts.append(f"Action: {action}")
        if result_message:
            parts.append(f"Result: {result_message}")
        if recommendation:
            parts.append(f"Recommendation: {recommendation}")
        if served:
            parts.append(f"Investigated by: {served}")
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


class NtfyNotificationSink(NotificationSink):
    """Deliver notifications to an ntfy topic (stdlib only).

    Every destination/render value — server URL, topic, title, per-severity
    priority and tags, auth token, request timeout — is injected by the caller
    (see ``bootstrap._build_notification_sinks``, which reads them from the
    ``observability.notifications`` config block or ``NTFY_*`` env vars).
    Nothing about the target is hard-coded here: the class-level ``DEFAULT_*``
    maps are overridable fallbacks, not fixed values.

    Message bodies reuse the shared ``_format_message`` so ntfy renders the
    same triage text as Slack/Discord; ntfy-native ``X-Priority``/``X-Tags``
    headers carry severity instead of inline emoji.
    """

    name = "ntfy-notification"

    # Overridable defaults. ntfy priority is 1 (min) .. 5 (max/urgent).
    DEFAULT_PRIORITY_MAP: Dict[str, str] = {
        "info": "3",
        "warning": "4",
        "critical": "5",
    }
    # ntfy renders these tag names as icons (see ntfy.sh/docs/emojis).
    DEFAULT_TAGS_MAP: Dict[str, str] = {
        "info": "information_source",
        "warning": "warning",
        "critical": "rotating_light",
    }
    DEFAULT_TITLE = "CFOperator Event Runtime"
    DEFAULT_PRIORITY = "3"

    def __init__(
        self,
        base_url: str,
        topic: str,
        *,
        title: str | None = None,
        priority_map: Dict[str, str] | None = None,
        tags_map: Dict[str, str] | None = None,
        token: str = "",
        timeout: int = 10,
        default_priority: str | None = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.topic = (topic or "").strip().strip("/")
        self.title = self.DEFAULT_TITLE if title is None else title
        self.priority_map = dict(self.DEFAULT_PRIORITY_MAP if priority_map is None else priority_map)
        self.tags_map = dict(self.DEFAULT_TAGS_MAP if tags_map is None else tags_map)
        self.token = token or ""
        self.timeout = timeout
        self.default_priority = str(self.DEFAULT_PRIORITY if default_priority is None else default_priority)

    @property
    def url(self) -> str:
        """Full publish URL, or empty string when not fully configured."""
        if not self.base_url or not self.topic:
            return ""
        return f"{self.base_url}/{self.topic}"

    def notify(self, summary: str, *, severity: str = "info", details: Dict | None = None) -> bool:
        # Inert until both url and topic are supplied (keeps the sink a no-op
        # when NTFY_URL/NTFY_TOPIC are unset rather than erroring).
        if not self.url:
            return False

        text = _format_message(summary, severity=severity, details=details)
        headers: Dict[str, str] = {"Content-Type": "text/plain; charset=utf-8"}
        if self.title:
            headers["X-Title"] = self.title
        priority = self.priority_map.get(severity, self.default_priority)
        if priority:
            headers["X-Priority"] = str(priority)
        tags = self.tags_map.get(severity)
        if tags:
            headers["X-Tags"] = tags
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        return self._post(text.encode("utf-8"), headers)

    def _post(self, data: bytes, headers: Dict[str, str]) -> bool:
        req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return 200 <= resp.status < 300
        except (urllib.error.URLError, OSError) as exc:
            logger.warning("ntfy notification failed: %s", exc)
            return False
