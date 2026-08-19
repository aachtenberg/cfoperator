"""Tests for the discovery entrypoint: model-reply parsing, learning
validation, the bounded characterization loop, the report, push mode, and the
component's standalone/tameness guards."""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from entrypoint import (
    MAX_EXTRA_QUERIES_PER_ROUND,
    characterize,
    parse_model_json,
    push_learnings,
    render_report,
    run,
    validate_learnings,
)
from inventory import QueryLog
from llm import LLM


def _learning(**over):
    base = {"learning_type": "insight", "title": "pi fleet", "description": "three Pis",
            "applies_when": "alerts on pi hosts", "services": ["k3s"], "tags": ["arm"],
            "confidence": 0.8}
    base.update(over)
    return base


class ScriptedLLM(LLM):
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.replies.pop(0)


# ---- parse_model_json --------------------------------------------------------


def test_parse_plain_and_fenced_json():
    assert parse_model_json('{"a": 1}') == {"a": 1}
    assert parse_model_json('Sure!\n```json\n{"a": 1}\n```\nHope that helps') == {"a": 1}
    assert parse_model_json('preamble {"a": {"b": 2}} trailing prose') == {"a": {"b": 2}}


def test_parse_rejects_no_json():
    with pytest.raises(ValueError):
        parse_model_json("I could not determine anything.")


# ---- validate_learnings ------------------------------------------------------


def test_missing_applies_when_is_dropped_loudly():
    """The KB born-deprecates trigger-less learnings; validation must refuse
    them here, visibly, instead of seeding dead weight that looks like
    success."""
    kept, dropped = validate_learnings([_learning(), _learning(applies_when="")])
    assert len(kept) == 1
    assert len(dropped) == 1
    assert "applies_when" in dropped[0]


def test_confidence_clamped_and_type_defaulted():
    kept, _ = validate_learnings([_learning(confidence=7, learning_type="prophecy")])
    assert kept[0]["confidence"] == 1.0
    assert kept[0]["learning_type"] == "insight"


def test_non_list_learnings_rejected():
    kept, dropped = validate_learnings("not a list")
    assert kept == [] and dropped


# ---- characterize loop bounds ------------------------------------------------


def _final_reply():
    return json.dumps({"overview_markdown": "## fleet", "learnings": [_learning()]})


def test_query_round_then_final():
    llm = ScriptedLLM([json.dumps({"queries": ["up"]}), _final_reply()])
    log = QueryLog()
    with patch("entrypoint.instant_query", return_value=[{"labels": {}, "value": "1"}]) as iq:
        reply = characterize(llm, {"prometheus": {}}, "http://prom", log, max_rounds=2)
    iq.assert_called_once_with("http://prom", "up", log)
    assert "learnings" in reply
    # Round 2's prompt carries the follow-up results back to the model.
    assert "followup_queries" in llm.prompts[1]


def test_loop_is_bounded_when_model_keeps_asking():
    """A model that never stops asking for queries gets max_rounds rounds, one
    forced-final call, and that's it — the pass is bounded, not a crawler."""
    asks = json.dumps({"queries": ["up"]})
    llm = ScriptedLLM([asks, asks, asks, _final_reply()])
    with patch("entrypoint.instant_query", return_value=[]):
        reply = characterize(llm, {}, "http://prom", QueryLog(), max_rounds=2)
    assert len(llm.prompts) == 4  # initial + 2 rounds + forced final
    assert "learnings" in reply
    assert "No further queries" in llm.prompts[-1]


def test_final_reply_with_empty_queries_is_not_looped():
    """Models often emit "queries": [] on a finished characterization; that
    must not trigger another round or the forced-final call."""
    final = json.loads(_final_reply())
    final["queries"] = []
    llm = ScriptedLLM([json.dumps(final)])
    with patch("entrypoint.instant_query") as iq:
        reply = characterize(llm, {}, "http://prom", QueryLog(), max_rounds=2)
    assert len(llm.prompts) == 1
    iq.assert_not_called()
    assert reply["learnings"]


def test_learnings_alongside_queries_are_not_discarded():
    """A reply carrying both keys is a finished characterization — looping
    would throw it away and re-ask."""
    final = json.loads(_final_reply())
    final["queries"] = ["up"]
    llm = ScriptedLLM([json.dumps(final)])
    with patch("entrypoint.instant_query") as iq:
        reply = characterize(llm, {}, "http://prom", QueryLog(), max_rounds=2)
    assert len(llm.prompts) == 1
    iq.assert_not_called()
    assert reply["learnings"][0]["title"] == "pi fleet"


def test_per_round_query_count_capped():
    many = json.dumps({"queries": [f"q{i}" for i in range(MAX_EXTRA_QUERIES_PER_ROUND + 10)]})
    llm = ScriptedLLM([many, _final_reply()])
    with patch("entrypoint.instant_query", return_value=[]) as iq:
        characterize(llm, {}, "http://prom", QueryLog(), max_rounds=1)
    assert iq.call_count == MAX_EXTRA_QUERIES_PER_ROUND


# ---- report ------------------------------------------------------------------


def test_report_contains_learnings_drops_and_query_appendix():
    log = QueryLog()
    log.record("prometheus", "GET /api/v1/targets", "ok")
    report = render_report("## overview", [_learning()], ["#1: missing applies_when"], log,
                           pushed=[42])
    assert "pi fleet" in report
    assert "stored as KB learning #42" in report
    assert "missing applies_when" in report
    assert "GET /api/v1/targets — ok" in report


# ---- push mode ---------------------------------------------------------------


class _CapturingHandler(BaseHTTPRequestHandler):
    captured = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _CapturingHandler.captured.append(
            {"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"id": len(_CapturingHandler.captured)}).encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def stub_agent():
    _CapturingHandler.captured = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CapturingHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", _CapturingHandler.captured
    server.shutdown()


def test_push_learnings_posts_with_token_and_provenance(stub_agent):
    url, captured = stub_agent
    ids = push_learnings(url, "tok-123", [_learning(), _learning(title="second")])
    assert ids == [1, 2]
    assert captured[0]["path"] == "/api/learnings"
    assert captured[0]["auth"] == "Bearer tok-123"
    assert captured[0]["body"]["source"] == "discovery"
    assert captured[0]["body"]["inferred"] is True
    assert captured[0]["body"]["applies_when"]


def test_run_report_only_without_agent_env(capsys):
    """The zero-commitment trial hook: no agent configured -> a report, no
    HTTP writes anywhere."""
    llm = ScriptedLLM([_final_reply()])
    with patch("entrypoint.enumerate_fleet", return_value={"prometheus": {}}), \
         patch("entrypoint.make_llm", return_value=llm), \
         patch("entrypoint.push_learnings") as push:
        rc = run({"PROMETHEUS_URL": "http://prom"})
    assert rc == 0
    push.assert_not_called()
    out = capsys.readouterr().out
    assert "Fleet discovery report" in out
    assert "What was queried" in out


def test_run_push_mode_failure_is_nonzero():
    llm = ScriptedLLM([_final_reply()])
    with patch("entrypoint.enumerate_fleet", return_value={"prometheus": {}}), \
         patch("entrypoint.make_llm", return_value=llm), \
         patch("entrypoint.push_learnings", return_value=[-1]):
        rc = run({"PROMETHEUS_URL": "http://prom", "CFOP_AGENT_URL": "http://agent",
                  "CFOP_API_TOKEN": "t"})
    assert rc == 1


def test_run_writes_report_files(tmp_path):
    llm = ScriptedLLM([_final_reply()])
    with patch("entrypoint.enumerate_fleet", return_value={"prometheus": {}}), \
         patch("entrypoint.make_llm", return_value=llm):
        rc = run({"PROMETHEUS_URL": "http://prom",
                  "CFOP_DISCOVERY_REPORT_DIR": str(tmp_path)})
    assert rc == 0
    assert (tmp_path / "report.md").exists()
    data = json.loads((tmp_path / "report.json").read_text())
    assert data["learnings"][0]["title"] == "pi fleet"


# ---- standalone / tameness guards (the CFOP-47 done-when) --------------------

_COMPONENT_FILES = sorted(Path(__file__).parent.glob("*.py"))


def test_component_never_imports_the_monolith_or_a_db():
    """Portable-component contract: no agent/ imports, no DB drivers. The KB
    schema stays private to the monolith; the only write path is HTTP."""
    banned = re.compile(
        r"^\s*(from|import)\s+(agent|knowledge_base|sqlalchemy|psycopg2?|cfshared)\b",
        re.MULTILINE)
    for f in _COMPONENT_FILES:
        assert not banned.search(f.read_text()), f"{f.name} imports a banned module"


def test_component_never_touches_ssh():
    """The discovery bounds promise no SSH, even with the hybrid overlay
    configured — the word should not even appear in the component."""
    for f in _COMPONENT_FILES:
        if f.name == Path(__file__).name:
            continue
        text = f.read_text().lower()
        assert "paramiko" not in text and "subprocess" not in text, \
            f"{f.name} references a process/SSH mechanism"
