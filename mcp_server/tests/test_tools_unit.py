"""Tools through the FastMCP layer with a stubbed client: scopes, payloads,
the ask_sre poll loop, and resource filtering."""

import asyncio
import json

import pytest

from mcp_server.client import CfopClient, UpstreamError
from mcp_server.config import Settings, expand_scopes
from mcp_server.server import build_server

ALL_SCOPES = frozenset({"read", "investigate", "remediate"})


class StubClient:
    """Records calls; per-method results seeded by tests.

    run_chat is the REAL CfopClient poll loop running over the stubbed
    start_chat/chat_events/stop_chat, so ask_sre tests exercise actual
    polling/timeout behavior.
    """

    run_chat = CfopClient.run_chat

    def __init__(self, settings=None):
        self._settings = settings or Settings()
        self.calls = []
        self.chat_event_batches = []

    async def triage(self, alert):
        self.calls.append(("triage", alert))
        return {"action": "monitor"}

    async def start_investigation(self, alert):
        self.calls.append(("start_investigation", alert))
        return {"status": "accepted", "alert_id": "a-1", "queue_depth": 1}

    async def list_investigations(self, limit=50):
        self.calls.append(("list_investigations", limit))
        return {"investigations": [{"id": 5}], "count": 1}

    async def get_investigation(self, investigation_id):
        self.calls.append(("get_investigation", investigation_id))
        return {"id": investigation_id}

    async def start_chat(self, message, history=None, backend="auto", model=None):
        self.calls.append(("start_chat", message))
        return {"chat_id": "abc123"}

    async def search_knowledge(self, query, limit=5):
        self.calls.append(("search_knowledge", query, limit))
        return {"query": query, "mode": "fts", "results": [{"title": "t"}], "count": 1}

    async def list_sweep_reports(self, limit=20):
        self.calls.append(("list_sweep_reports", limit))
        return {"reports": [
            {"swept_at": "2026-07-29T12:00:00", "summary": "Sweep",
             "sweep_meta": {"type": "scheduled"}, "findings": []},
            {"swept_at": "2026-07-29T11:00:00", "summary": "Morning summary - 2026-07-29",
             "sweep_meta": {"type": "morning_summary", "full_text": "all quiet on the fleet"},
             "findings": [{"finding": "all quiet"}]},
        ]}

    async def chat_events(self, chat_id, cursor=0):
        self.calls.append(("chat_events", chat_id, cursor))
        if self.chat_event_batches:
            return self.chat_event_batches.pop(0)
        return {"events": [], "cursor": cursor, "done": False}

    async def stop_chat(self, chat_id):
        self.calls.append(("stop_chat", chat_id))
        return {"status": "stopping"}

    async def list_remediations(self, status=None, limit=50):
        self.calls.append(("list_remediations", status, limit))
        return {"remediations": [
            {"id": 1, "status": "queued"},
            {"id": 2, "status": "resolved"},
            {"id": 3, "status": "needs-human"},
        ], "count": 3}

    async def get_remediation(self, remediation_id):
        self.calls.append(("get_remediation", remediation_id))
        return {"id": remediation_id}

    async def approve_remediation(self, remediation_id):
        self.calls.append(("approve_remediation", remediation_id))
        return {"id": remediation_id, "status": "queued"}

    async def reject_remediation(self, remediation_id, note=None):
        self.calls.append(("reject_remediation", remediation_id, note))
        return {"id": remediation_id, "status": "rejected"}


def call(server, tool, args):
    result = asyncio.run(server.call_tool(tool, args))
    return json.loads(result[0].text)


def make(scopes=ALL_SCOPES, **settings_kwargs):
    settings = Settings(scopes=frozenset(scopes), **settings_kwargs)
    stub = StubClient(settings)
    return build_server(settings, client=stub), stub


# --- config ---

def test_scope_expansion_hierarchy():
    assert expand_scopes(["read"]) == frozenset({"read"})
    assert expand_scopes(["investigate"]) == frozenset({"read", "investigate"})
    assert expand_scopes(["remediate"]) == ALL_SCOPES
    with pytest.raises(ValueError):
        expand_scopes(["admin"])
    with pytest.raises(ValueError):
        expand_scopes([""])


def test_http_transport_requires_token():
    env = {"CFOP_MCP_TRANSPORT": "http"}
    with pytest.raises(ValueError, match="CFOP_MCP_TOKEN"):
        Settings.from_env(env)
    ok = Settings.from_env({"CFOP_MCP_TRANSPORT": "http", "CFOP_MCP_TOKEN": "t"})
    assert ok.transport == "streamable-http"


# --- scope gating ---

def test_read_scope_cannot_triage():
    server, stub = make(scopes={"read"})
    out = call(server, "triage_alert", {"summary": "pod down"})
    assert out["error"]["code"] == "unauthorized"
    assert stub.calls == []


def test_investigate_scope_cannot_approve():
    server, stub = make(scopes={"read", "investigate"})
    out = call(server, "approve_remediation", {"remediation_id": 1})
    assert out["error"]["code"] == "unauthorized"
    assert stub.calls == []


def test_remediate_scope_can_approve():
    server, stub = make()
    out = call(server, "approve_remediation", {"remediation_id": 1})
    assert out == {"id": 1, "status": "queued"}
    assert stub.calls == [("approve_remediation", 1)]


# --- payload assembly ---

def test_start_investigation_builds_alert_payload():
    server, stub = make()
    out = call(server, "start_investigation", {
        "summary": "pod down",
        "severity": "critical",
        "labels": {"node": "pi5"},
        "alert_id": "alert-9",
        "idempotency_key": "k-1",
    })
    assert out["status"] == "accepted"
    (_, alert), = stub.calls
    assert alert == {
        "summary": "pod down",
        "severity": "critical",
        "labels": {"node": "pi5"},
        "alert_id": "alert-9",
        "idempotency_key": "k-1",
    }


# --- ask_sre poll loop ---

def test_ask_sre_collects_events_until_done():
    server, stub = make(chat_poll_interval=0.01)
    stub.chat_event_batches = [
        {"events": [{"event": "tool_call", "data": {"name": "query_metrics"}}],
         "cursor": 1, "done": False},
        {"events": [{"event": "tool_result", "data": {}},
                    {"event": "done", "data": {"response": "pi5 is fine"}}],
         "cursor": 3, "done": True},
    ]
    out = call(server, "ask_sre", {"question": "how is pi5?"})
    assert out == {"chat_id": "abc123", "response": "pi5 is fine", "tool_calls": 1}
    # second poll resumed from the first batch's cursor
    assert ("chat_events", "abc123", 1) in stub.calls


def test_ask_sre_timeout_stops_chat_and_returns_retryable_error():
    server, stub = make(chat_poll_interval=0.01, chat_timeout=0.05)
    out = call(server, "ask_sre", {"question": "slow one"})
    assert out["error"]["code"] == "upstream_unavailable"
    assert out["error"]["retryable"] is True
    assert ("stop_chat", "abc123") in stub.calls


def test_ask_sre_error_event_surfaces_as_error_payload():
    server, stub = make(chat_poll_interval=0.01)
    stub.chat_event_batches = [
        {"events": [{"event": "error", "data": {"error": "LLM backend down"}}],
         "cursor": 1, "done": True},
    ]
    out = call(server, "ask_sre", {"question": "x"})
    assert out["error"]["code"] == "upstream_unavailable"
    assert "LLM backend down" in out["error"]["message"]


# --- upstream error passthrough ---

def test_upstream_error_becomes_structured_payload():
    server, stub = make()

    async def boom(investigation_id):
        raise UpstreamError("not_found", "not found", retryable=False, status=404)

    stub.get_investigation = boom
    out = call(server, "get_investigation", {"investigation_id": 42})
    assert out["error"] == {
        "code": "not_found", "message": "not found", "retryable": False}


# --- resources ---

def test_open_remediations_resource_filters_terminal_rows():
    server, stub = make()
    contents = asyncio.run(server.read_resource("cfop://remediations/open"))
    payload = json.loads(contents[0].content)
    assert payload["count"] == 2
    assert {r["id"] for r in payload["remediations"]} == {1, 3}


def test_investigation_detail_resource():
    server, stub = make()
    contents = asyncio.run(server.read_resource("cfop://investigations/17"))
    assert json.loads(contents[0].content) == {"id": 17}
    assert ("get_investigation", 17) in stub.calls


# --- phase 2: knowledge, digest, prompts ---

def test_search_knowledge_is_read_scoped():
    server, stub = make(scopes={"read"})
    out = call(server, "search_knowledge", {"query": "etcd timeout"})
    assert out["count"] == 1
    assert ("search_knowledge", "etcd timeout", 5) in stub.calls


def test_morning_digest_resource_picks_morning_summary_full_text():
    server, stub = make()
    contents = asyncio.run(server.read_resource("cfop://digest/morning"))
    payload = json.loads(contents[0].content)
    assert payload["text"] == "all quiet on the fleet"
    assert payload["summary"].startswith("Morning summary")


def test_prompts_registered_from_skills_dir():
    server, _ = make()
    prompts = asyncio.run(server.list_prompts())
    names = {p.name for p in prompts}
    assert "investigate-pod" in names and "why-restart" in names
    rendered = asyncio.run(server.get_prompt(
        "investigate-pod", {"target": "apps/foo"}))
    assert "apps/foo" in rendered.messages[0].content.text


# --- audit logging ---

def test_tool_calls_emit_audit_lines(caplog):
    server, stub = make(scopes={"read"})
    with caplog.at_level("INFO", logger="mcp_server.audit"):
        call(server, "list_investigations", {"limit": 1})   # ok
        call(server, "triage_alert", {"summary": "x"})      # unauthorized
    lines = [json.loads(r.message) for r in caplog.records
             if r.name == "mcp_server.audit"]
    assert [(l["tool"], l["outcome"]) for l in lines] == [
        ("list_investigations", "ok"),
        ("triage_alert", "unauthorized"),
    ]
    assert all("ms" in l and l["scope"] for l in lines)


# --- bearer middleware (raw ASGI) ---

def _asgi_probe(auth_header=None):
    from mcp_server.auth import BearerAuthMiddleware

    reached = []

    async def inner(scope, receive, send):
        reached.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent = []

    async def send(message):
        sent.append(message)

    headers = [(b"authorization", auth_header.encode())] if auth_header else []
    scope = {"type": "http", "headers": headers}
    mw = BearerAuthMiddleware(inner, token="s3cret")
    asyncio.run(mw(scope, lambda: None, send))
    return reached, sent


def test_middleware_rejects_missing_and_wrong_tokens():
    for bad in (None, "Bearer nope", "Basic s3cret", "s3cret"):
        reached, sent = _asgi_probe(bad)
        assert reached == []
        assert sent[0]["status"] == 401
        assert json.loads(sent[1]["body"])["code"] == "unauthorized"


def test_middleware_passes_valid_token():
    reached, sent = _asgi_probe("Bearer s3cret")
    assert reached == [True]
    assert sent[0]["status"] == 200
