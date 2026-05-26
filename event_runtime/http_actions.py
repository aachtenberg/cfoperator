"""HTTP-backed action handlers that delegate to an external service.

This is the event_runtime side of the alert pipeline unification. Today the
default ``investigate`` action is a stub that just records that an alert was
seen (``InvestigateActionHandler`` in defaults.py). The handler here replaces
that stub when the agent is reachable: it POSTs the alert to the agent's
``/v1/investigate`` endpoint, returns a quiet (non-notifying) ``ActionResult``
acknowledging the queued investigation, and lets the agent post the real
outcome back to ``POST /v1/investigations/{alert_id}/complete``. That
completion path fires the actual Slack notification through the normal
``_notify_action_completed`` flow.

The handler is registered from ``bootstrap`` only when ``CFOP_AGENT_URL`` is
set, so portable deployments without the agent keep the stub.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Optional

from .models import ActionRequest, ActionResult
from .plugins import ActionHandler

logger = logging.getLogger(__name__)


class HTTPInvestigateActionHandler(ActionHandler):
    """Delegate ``investigate`` actions to an external agent over HTTP.

    The agent is expected to:
      1. Accept ``POST {agent_url}/v1/investigate`` with the alert body
         (matches event_runtime.models.Alert.to_dict()).
      2. Return 2xx for accepted, 5xx (or refuse the connection) when it
         can't take the work — the worker retries the latter.
      3. POST the completed ActionResult back to
         ``{event_runtime_url}/v1/investigations/{alert_id}/complete``
         when the LLM run finishes.

    The handler itself returns a quiet ActionResult so Slack only fires once,
    on completion, not twice (queued + done).
    """

    name = "http-investigate-action"
    action_name = "investigate"

    def __init__(self, agent_url: str, *, timeout: float = 5.0):
        if not agent_url:
            raise ValueError("agent_url is required")
        self._agent_url = agent_url.rstrip("/")
        self._timeout = float(timeout)

    @property
    def agent_url(self) -> str:
        return self._agent_url

    def execute(self, request: ActionRequest) -> ActionResult:
        endpoint = f"{self._agent_url}/v1/investigate"
        body = json.dumps(request.alert.to_dict(), default=str).encode("utf-8")
        try:
            self._post(endpoint, body)
        except urllib.error.HTTPError as exc:
            # 4xx is permanent (bad payload); surface as failure-without-retry.
            # 5xx is transient; raising lets the worker retry per its policy.
            if 500 <= exc.code < 600:
                logger.warning("Agent returned %s for investigate dispatch: %s", exc.code, endpoint)
                raise
            return ActionResult(
                action=self.action_name,
                success=False,
                message=f"Agent rejected investigation ({exc.code})",
                details={
                    "agent_url": self._agent_url,
                    "alert_id": request.alert.alert_id,
                    "http_status": exc.code,
                },
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.warning("Agent unreachable for investigate dispatch: %s (%s)", endpoint, type(exc).__name__)
            raise

        return ActionResult(
            action=self.action_name,
            success=True,
            message=f"Investigation dispatched to agent for: {request.alert.summary}",
            details={
                "agent_url": self._agent_url,
                "alert_id": request.alert.alert_id,
            },
            quiet=True,  # The completion post-back will fire the real Slack notification.
        )

    def _post(self, endpoint: str, body: bytes) -> None:
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            # Drain the response so the connection can be reused; status is
            # the only thing we care about beyond exception flow above.
            resp.read()


def build_http_investigate_handler(agent_url: Optional[str]) -> Optional[HTTPInvestigateActionHandler]:
    """Construct the HTTP investigate handler when an agent URL is configured.

    Returns None when ``agent_url`` is missing or empty, so callers can fall
    back to the default stub.
    """
    if not agent_url:
        return None
    return HTTPInvestigateActionHandler(agent_url=agent_url)
