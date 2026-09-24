"""Writing investigation results back onto Dynatrace problems (CFOP-206).

The alert is the one the real problem source emits for the live P-26091 row,
and the result has the shape the agent posts back (agent._build_action_result).
"""

from __future__ import annotations

import io
import json
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit

import pytest

from event_runtime.engine import EventRuntime
from event_runtime.escalation import EscalationLedger
from event_runtime.external_plugins import PluginContext, load_external_plugins
from event_runtime.models import ActionResult, Alert, AlertSeverity
from event_runtime.plugin_manager import PluginManager
from integrations.dynatrace import writeback
from integrations.dynatrace.test_evidence import alert_from
from integrations.dynatrace.test_problems import P_26091
from integrations.dynatrace.writeback import DynatraceProblemCommenter, classic_api_url

API = "https://abc12345.live.dynatrace.com"
TOKEN = "dt0c01.TESTTOKEN.SECRETPART"
PROBLEM_ID = "-6597882896083206449_1790268660000V2"

RESULT = ActionResult(
    action="investigate",
    success=True,
    message="Action needed: Dynatrace P-26091: Backoff event on dt-chaos/crashloop (43.9s, 3 tool calls)",
    details={
        "investigation_id": 6,
        "outcome": "needs_action",
        "duration_s": 43.9,
        "tool_calls": 3,
        "provider": "ollama/gemma4:26b",
        "findings_snippet": "The crashloop deployment in dt-chaos is intentionally\n configured to fail.",
        "remediation": "kubectl scale deployment crashloop -n dt-chaos --replicas=0",
    },
)


class Capture:
    """Stands in for urlopen: records requests, answers from a script."""

    def __init__(self, *answers):
        self.answers = list(answers) or [201]
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append({
            "url": request.full_url,
            "method": request.get_method(),
            "headers": {k.lower(): v for k, v in request.header_items()},
            "body": json.loads(request.data),
        })
        answer = self.answers.pop(0) if self.answers else 201
        if isinstance(answer, Exception):
            raise answer
        if answer >= 400:
            raise HTTPError(request.full_url, answer, "error", {}, io.BytesIO(b'{"error":{"code":403,"message":"Token is missing required scope"}}'))
        return io.BytesIO(b"{}")


@pytest.fixture
def capture(monkeypatch):
    def install(*answers):
        cap = Capture(*answers)
        monkeypatch.setattr(writeback, "urlopen", cap)
        return cap
    return install


def commenter():
    return DynatraceProblemCommenter(API, TOKEN)


def test_an_investigation_result_becomes_a_comment_on_its_problem(capture):
    cap = capture()
    commenter().observe(alert_from(P_26091), RESULT)
    (req,) = cap.requests
    assert req["method"] == "POST"
    assert unquote(urlsplit(req["url"]).path) == f"/api/v2/problems/{PROBLEM_ID}/comments"
    assert req["headers"]["authorization"] == f"Api-Token {TOKEN}"
    assert req["body"]["context"] == "cfoperator"
    message = req["body"]["message"]
    assert message.startswith("cfoperator investigated this problem (investigation #6, outcome: needs_action).")
    assert "Recommendation: kubectl scale deployment crashloop -n dt-chaos --replicas=0" in message
    assert "Summary: The crashloop deployment in dt-chaos is intentionally configured to fail." in message
    assert "Model: ollama/gemma4:26b" in message


def test_the_problem_id_is_quoted_into_the_path(capture):
    cap = capture()
    alert = alert_from(P_26091)
    alert.fingerprint = "dynatrace:a/b?c"
    commenter().observe(alert, RESULT)
    assert "/api/v2/problems/a%2Fb%3Fc/comments" in cap.requests[0]["url"]


def test_each_investigation_is_written_once(capture):
    cap = capture()
    c, alert = commenter(), alert_from(P_26091)
    c.observe(alert, RESULT)
    c.observe(alert, RESULT)           # a repeated post-back
    assert len(cap.requests) == 1


def test_two_racing_post_backs_of_one_investigation_post_once(monkeypatch):
    """Completions arrive on server threads; the second must not pass the check mid-POST."""
    import threading

    requests, second_done = [], threading.Event()

    def slow_urlopen(request, timeout=None):
        requests.append(request.full_url)
        second_done.wait(timeout=2)            # hold the first POST open while the second arrives
        return io.BytesIO(b"{}")

    monkeypatch.setattr(writeback, "urlopen", slow_urlopen)
    c, alert = commenter(), alert_from(P_26091)
    first = threading.Thread(target=c.observe, args=(alert, RESULT))
    first.start()
    while not requests:                         # wait until the first POST is in flight
        pass

    def second():
        c.observe(alert, RESULT)
        second_done.set()

    other = threading.Thread(target=second)
    other.start()
    other.join(timeout=5)
    first.join(timeout=5)
    assert len(requests) == 1


@pytest.mark.parametrize("alert,result", [
    (Alert(source="alertmanager", severity=AlertSeverity.WARNING, summary="x", fingerprint="dynatrace:1"), RESULT),
    (Alert(source="dynatrace", severity=AlertSeverity.WARNING, summary="x", fingerprint="am:1"), RESULT),
    (Alert(source="dynatrace", severity=AlertSeverity.INFO, summary="Resolved: x", fingerprint="dynatrace:1",
           details={"resolution": True}), RESULT),
    (None, ActionResult(action="notify", success=True, message="notified", details={"investigation_id": 1})),
    (None, ActionResult(action="investigate", success=True, message="Investigation dispatched to agent",
                        details={"agent_url": "http://agent:8083"})),
])
def test_only_investigation_results_for_dynatrace_problems_are_written(capture, alert, result):
    cap = capture()
    commenter().observe(alert or alert_from(P_26091), result)
    assert cap.requests == []


def test_a_rejected_comment_is_logged_not_raised_and_can_be_retried_later(capture, caplog):
    cap = capture(403, 201)
    c, alert = commenter(), alert_from(P_26091)
    with caplog.at_level("WARNING"):
        c.observe(alert, RESULT)
    assert "Could not write investigation #6 back to Dynatrace problem P-26091: HTTP 403" in caplog.text
    c.observe(alert, RESULT)           # not remembered as written, so the next post-back tries again
    assert len(cap.requests) == 2


def test_a_network_failure_is_not_retried_because_a_comment_is_not_idempotent(capture):
    cap = capture(URLError(ConnectionResetError(104, "Connection reset by peer")))
    commenter().observe(alert_from(P_26091), RESULT)
    assert len(cap.requests) == 1


def test_a_long_result_is_trimmed():
    long = ActionResult(action="investigate", success=True, message="m",
                        details={"investigation_id": 1, "findings_snippet": "y" * 5000})
    text = commenter()._message(long, long.details, 1)
    assert len(text) <= 2000 and text.endswith("[... trimmed]")


def test_the_classic_host_is_derived_for_saas_and_required_otherwise():
    assert classic_api_url("https://abc12345.apps.dynatrace.com/") == API
    with pytest.raises(ValueError, match="set DT_API_URL"):
        classic_api_url("https://dynatrace.example.com/e/abc")


def test_a_platform_token_is_refused_at_startup():
    with pytest.raises(ValueError, match="needs? a classic access token|comments need a classic access token"):
        DynatraceProblemCommenter(API, "dt0s16.PLATFORM.TOKEN")


def test_the_token_stays_out_of_repr():
    assert "SECRETPART" not in repr(commenter())


# --- register() and the runtime -------------------------------------------------

BASE_ENV = {"DT_ENVIRONMENT_URL": "https://abc12345.apps.dynatrace.com", "DT_PLATFORM_TOKEN": "dt0s16.X.Y"}


def _load(monkeypatch, **env):
    for key in ("DT_PROBLEMS_TOKEN", "DT_API_URL", "CFOP_DYNATRACE_EVIDENCE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**BASE_ENV, **env}.items():
        monkeypatch.setenv(key, value)
    plugins = PluginManager()
    load_external_plugins(plugins, PluginContext(escalation_ledger=EscalationLedger()), raw="integrations.dynatrace")
    return plugins


def test_without_a_write_token_nothing_is_written(monkeypatch):
    assert _load(monkeypatch).completion_observers == []


def test_with_a_write_token_the_commenter_is_registered_on_the_classic_host(monkeypatch):
    (observer,) = _load(monkeypatch, DT_PROBLEMS_TOKEN=TOKEN).completion_observers
    assert isinstance(observer, DynatraceProblemCommenter) and observer.api_url == API


def test_an_explicit_api_url_wins(monkeypatch):
    (observer,) = _load(monkeypatch, DT_PROBLEMS_TOKEN=TOKEN,
                        DT_API_URL="https://managed.example.com/e/abc").completion_observers
    assert observer.api_url == "https://managed.example.com/e/abc"


def test_a_resolved_outcome_is_written_back_even_though_the_digest_holds_its_page(monkeypatch, capture):
    """The reason this is an observer and not a sink, through the real engine."""
    from event_runtime.models import Decision
    from event_runtime.plugins import DecisionEngine, NotificationSink, StateSink

    class Decide(DecisionEngine):
        name = "decide"

        def decide(self, envelope):
            return Decision(action="investigate", confidence=1.0, reasoning="test")

    class Pager(NotificationSink):
        name = "pager"
        pages = []

        def notify(self, summary, *, severity="info", details=None):
            self.pages.append(summary)
            return True

    class Memory(StateSink):
        name = "memory"

        def append(self, events):
            pass

        def recent(self, limit=50):
            return []

        def health(self):
            return {}

    monkeypatch.delenv("CFOP_DIGEST_LOW_SEVERITY", raising=False)
    cap = capture()
    plugins = _load(monkeypatch, DT_PROBLEMS_TOKEN=TOKEN)
    plugins.register_state_sink(Memory())
    plugins.register_decision_engine(Decide())
    pager = Pager()
    plugins.register_notification_sink(pager)
    alert = alert_from(dict(P_26091, **{"event.category": "SLOWDOWN"}))       # a warning, not critical
    resolved = ActionResult(action="investigate", success=True, message="Resolved: slowdown cleared",
                            details={"investigation_id": 9, "outcome": "resolved"})

    EventRuntime(plugins).record_external_action_completion(alert, resolved)

    assert pager.pages == []                                  # the digest kept it from paging
    (req,) = cap.requests                                     # but the problem still got its answer
    assert "outcome: resolved" in req["body"]["message"]
