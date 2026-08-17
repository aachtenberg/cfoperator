"""Guards for the first-run demo (scripts/demo-alert.sh).

The class of regression: the demo is the first thing a stranger runs, and every
way it can fail is silent until someone is already watching. Two failures in
particular cannot be caught by running it on a machine that happens to work:

  - The alert payload drifts from what event_runtime accepts, so the demo dies
    at ingest with an HTTP 400 instead of showing anything.
  - The attach line it prints stops matching the verb cfassist implements, so
    the last and most important step tells the operator to paste a command that
    does not exist.

Both are checked here against the real `Alert.from_dict` and the real Go
source, rather than against a copy of what they used to say.
"""

import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent
SCRIPT = REPO / "scripts" / "demo-alert.sh"


def _embedded_python() -> str:
    """The heredoc body the script runs inside the container."""
    body = SCRIPT.read_text()
    match = re.search(r"<<'PY'\n(.*?)\nPY\n", body, re.S)
    assert match, "the embedded python block moved; reread demo-alert.sh"
    return match.group(1)


def _demo_payload() -> dict:
    """Rebuild the payload the script posts, from the script itself.

    Executing the literal keeps this honest: if someone edits the dict in the
    script, this test sees the edit rather than a stale copy of it.
    """
    py = _embedded_python()
    match = re.search(r"payload = (\{.*?\n\})", py, re.S)
    assert match, "payload literal not found in demo-alert.sh"
    literal = match.group(1)
    # The literal interpolates a few locals; substitute representative values so
    # the shape (not the content) is what gets validated.
    for name, value in (
        ("severity", '"critical"'),
        ("summary", '"target x is down"'),
        ("inst", '"127.0.0.1:9090"'),
        ("job", '"prometheus"'),
    ):
        literal = re.sub(rf"\b{name}\b(?!\s*[\"':])", value, literal)
    return eval(literal)  # noqa: S307 - our own source, reconstructed above


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file(), "scripts/demo-alert.sh is missing"
    assert SCRIPT.stat().st_mode & 0o111, "demo-alert.sh is not executable"


def test_embedded_python_parses():
    """A syntax error here is invisible until someone runs the demo."""
    import ast

    ast.parse(_embedded_python())


def test_payload_is_one_event_runtime_accepts():
    """The demo must not die at ingest.

    Validated against the real model rather than a schema copy, so a field
    rename in event_runtime fails here instead of in front of a trial user.
    """
    from event_runtime.models import Alert

    alert = Alert.from_dict(_demo_payload())
    assert alert.summary, "summary is what triage reads; it cannot be empty"
    assert alert.severity is not None
    assert alert.alert_id, "from_dict must mint an id the agent can reference"


def test_demo_does_not_publish_the_alert_injection_port():
    """POST /alert is unauthenticated in the trial (CFOP_RUNTIME_TOKEN unset).

    The script reaches it via `docker compose exec` on purpose. If someone
    'simplifies' this by publishing 8080, the trial gains an unauthenticated
    write endpoint on the operator's LAN.
    """
    import yaml

    # Parse the compose rather than splitting on the service name: the agent
    # service carries `CFOP_EVENT_RUNTIME_URL: http://event-runtime:8080`, so a
    # naive split lands inside that value and then scans the rest of the file.
    # That version passed, but for the wrong reason — it would equally fire on
    # any *other* service publishing 8080, and would keep passing if this one
    # published a different sensitive port.
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    runtime = compose["services"]["event-runtime"]

    published = []
    for entry in runtime.get("ports") or []:
        text = entry if isinstance(entry, str) else str(entry)
        published.append(text.rsplit(":", 1)[-1].strip('"'))

    assert not published, (
        "event_runtime publishes a host port in the trial compose "
        f"({published}). POST /alert is unauthenticated there — "
        "CFOP_RUNTIME_TOKEN is unset — so this puts an alert-injection "
        "endpoint on the operator's LAN. The demo reaches it via "
        "`docker compose exec` for exactly this reason."
    )
    script = SCRIPT.read_text()
    assert "compose" in script and "exec" in script, (
        "the demo no longer reaches event_runtime through compose exec"
    )


def test_attach_line_matches_the_verb_cfassist_implements():
    """The demo's final step is the cockpit handoff. If the verb drifts, the
    demo ends by telling the operator to paste something that does not exist.

    Checks the Go source, which is the released binary — see CFOP-29.
    """
    printed = re.search(r"cfassist (\w+) \{inv_id\}", _embedded_python())
    assert printed, "the attach line vanished from demo-alert.sh"
    verb = printed.group(1)

    briefing = (REPO / "cfassist-go" / "internal" / "cfoperator" / "briefing.go").read_text()
    declared = re.search(r'AttachVerb\s*=\s*"(\w+)"', briefing)
    assert declared, "AttachVerb not found in cfassist-go; reread briefing.go"

    assert verb == declared.group(1), (
        f"the demo tells operators to run 'cfassist {verb}' but cfassist-go "
        f"implements '{declared.group(1)}'"
    )


def test_investigation_is_correlated_not_assumed():
    """The demo must report *its own* investigation.

    Polling for the newest row attributes whatever finished last to this demo —
    so a real alert landing mid-run makes it print an attach line pointing at
    someone else's incident. Investigation rows carry no alert_id, so the match
    is trigger text plus a pre-submit id baseline.
    """
    py = _embedded_python()
    assert "baseline" in py, "the pre-submit id baseline is gone"
    assert re.search(r"id.*<=\s*baseline", py), (
        "nothing rejects investigations that predate this run"
    )
    # The completion poll must consider a window and pick by correlation, not
    # take whatever is newest. (The *baseline* query legitimately uses limit=1 —
    # it wants exactly the newest id, before anything is submitted.)
    assert "?limit=10" in py, (
        "the completion poll no longer fetches a window to correlate against"
    )
    helper = re.search(r"def find_ours\(rows\):\n(.*?)\n(?=\S)", py, re.S)
    assert helper, "the correlation helper is gone"
    body = helper.group(1)

    assert "trigger == summary" in body, "the exact-match correlation is gone"
    # Scoped to the helper on purpose: `startswith` is used legitimately
    # elsewhere in this script (parsing the token env file). Only a loose
    # comparison *here* matters — a prefix match reintroduces the bug this
    # helper exists to prevent, since a real investigation whose trigger is a
    # prefix of our summary ("Prometheus target" would do) passes both the id
    # and the prefix test, and wins.
    assert "startswith" not in body, (
        "correlation went back to a prefix match — a shorter real trigger will "
        "match this demo's summary and the attach line will point at it"
    )


def test_alert_is_discovered_not_fabricated():
    """Guards the design decision, not the wording.

    A fabricated alert about infrastructure a trial does not have can only end
    in 'I could not check' — that is exactly what the first attempt at this
    demo did (CFOP-25 boot test: a made-up CrashLoop, correctly escalated,
    useless). The script must consult Prometheus before inventing anything.
    """
    py = _embedded_python()
    assert "up == 0" in py, "the script no longer looks for a real fault"
    assert "up == 1" in py, "the script no longer has a healthy-target fallback"
    for invented in ("CrashLoopBackOff", "checkout-api", "OOMKilled"):
        assert invented not in py, (
            f"{invented!r} is fabricated infrastructure; a trial has no "
            "Kubernetes and the investigation can only escalate"
        )
