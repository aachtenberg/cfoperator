"""MCP connectors (CFOP-443): stored shape, policy, and a real round trip.

The end-to-end class runs a real MCP server (FastMCP over streamable HTTP,
behind a header check) on a loopback port and drives it through the same
client the chat uses. Mocks alone would prove the glue, not the client.
"""

import json
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools import UNATTENDED, ToolPolicy, ToolRegistry
from tools import mcp_connectors as mc

ADMIN = ToolPolicy(actor_role="admin")
MEMBER = ToolPolicy(actor_role="member")
VERIFY = ToolPolicy(actor_role="admin", verify_only=True)


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("CFOP_SESSION_SECRET", "test-session-secret-for-connectors")


def _registry():
    op = MagicMock()
    op.config = {"infrastructure": {"hosts": {}}, "search": {}}
    return op, ToolRegistry(op)


def _tool(name, read_only=None, schema=None, description="does a thing"):
    annotations = None if read_only is None else SimpleNamespace(readOnlyHint=read_only)
    return SimpleNamespace(name=name, description=description, annotations=annotations,
                           inputSchema=schema or {"type": "object", "properties": {}})


def _connector(name="plane", **kw):
    entry = {"name": name, "url": "https://mcp.example/mcp", "enabled": True, "tools": [],
             "headers": "", "header_names": []}
    entry.update(kw)
    return entry


# ── Stored shape ─────────────────────────────────────────────────────────


class TestParseInput:
    def test_a_new_connector_needs_a_url(self):
        with pytest.raises(mc.ConnectorError):
            mc.parse_input("plane", {}, None)

    @pytest.mark.parametrize("url", ["ftp://x/mcp", "plane:8211/mcp", "", "https://"])
    def test_only_http_urls(self, url):
        with pytest.raises(mc.ConnectorError):
            mc.parse_input("plane", {"url": url}, None)

    def test_credentials_in_the_url_are_refused(self):
        # The url is shown to every console user; a key belongs in a header.
        with pytest.raises(mc.ConnectorError, match="header"):
            mc.parse_input("plane", {"url": "https://u:key@mcp.example/mcp"}, None)

    @pytest.mark.parametrize("name", ["Plane", "1plane", "pl_ane", "a" * 17, ""])
    def test_names_are_short_and_plain(self, name):
        with pytest.raises(mc.ConnectorError):
            mc.parse_input(name, {"url": "https://mcp.example/mcp"}, None)

    def test_headers_are_sealed_and_their_names_kept(self, secret):
        entry = mc.parse_input("plane", {"url": "https://mcp.example/mcp",
                                         "headers": "x-api-key: s3cret-value\nx-workspace-slug: cfop"},
                               None)
        assert "s3cret-value" not in json.dumps(entry)
        assert entry["header_names"] == ["x-api-key", "x-workspace-slug"]
        assert mc.headers_for(entry) == ({"x-api-key": "s3cret-value", "x-workspace-slug": "cfop"}, None)

    def test_omitted_headers_keep_what_is_stored(self, secret):
        first = mc.parse_input("plane", {"url": "https://mcp.example/mcp",
                                         "headers": {"x-api-key": "k1"}}, None)
        edited = mc.parse_input("plane", {"url": "https://other.example/mcp", "enabled": False}, first)
        assert edited["headers"] == first["headers"]
        assert edited["url"] == "https://other.example/mcp" and edited["enabled"] is False

    def test_empty_headers_clear_them(self, secret):
        first = mc.parse_input("plane", {"url": "https://mcp.example/mcp",
                                         "headers": {"x-api-key": "k1"}}, None)
        cleared = mc.parse_input("plane", {"headers": {}}, first)
        assert cleared["headers"] == "" and cleared["header_names"] == []

    def test_headers_without_a_secret_are_not_stored_in_the_clear(self, monkeypatch):
        monkeypatch.delenv("CFOP_SESSION_SECRET", raising=False)
        with pytest.raises(mc.sealed.SealUnavailable):
            mc.parse_input("plane", {"url": "https://mcp.example/mcp",
                                     "headers": {"x-api-key": "k1"}}, None)

    @pytest.mark.parametrize("headers", ["no colon here", {"bad name": "v"}, {"x": "a\r\nInjected: 1"}])
    def test_malformed_headers_are_refused(self, secret, headers):
        with pytest.raises(mc.ConnectorError):
            mc.parse_input("plane", {"url": "https://mcp.example/mcp", "headers": headers}, None)

    def test_a_rotated_secret_reads_as_unreadable_not_as_garbage(self, secret, monkeypatch):
        entry = mc.parse_input("plane", {"url": "https://mcp.example/mcp",
                                         "headers": {"x-api-key": "k1"}}, None)
        monkeypatch.setenv("CFOP_SESSION_SECRET", "rotated")
        headers, why = mc.headers_for(entry)
        assert headers == {} and "decrypt" in why
        assert mc.public_view(entry)["headers_state"] == "unreadable"

    def test_the_public_view_carries_no_header_value(self, secret):
        entry = mc.parse_input("plane", {"url": "https://mcp.example/mcp",
                                         "headers": {"x-api-key": "s3cret-value"}}, None)
        view = mc.public_view(entry, {"state": "ok"})
        assert "s3cret-value" not in json.dumps(view)
        assert "headers" not in view and view["header_names"] == ["x-api-key"]


class TestStoredList:
    def test_loads_drops_what_it_cannot_trust(self):
        raw = json.dumps([_connector("plane"), {"name": "Bad!"}, "junk", _connector("plane"),
                          _connector("grafana")])
        assert [c["name"] for c in mc.loads(raw)] == ["plane", "grafana"]

    @pytest.mark.parametrize("raw", ["", None, "not json", "{}", MagicMock()])
    def test_loads_never_raises(self, raw):
        assert mc.loads(raw) == []

    def test_upsert_edits_in_place_and_remove_reports(self):
        current = [_connector("a"), _connector("b")]
        updated = mc.upsert(current, _connector("a", url="https://new.example/mcp"))
        assert [c["name"] for c in updated] == ["a", "b"]
        assert updated[0]["url"] == "https://new.example/mcp"
        assert mc.remove(updated, "b")[1] is True
        assert mc.remove(updated, "zzz")[1] is False


# ── Schemas and results ──────────────────────────────────────────────────


class TestCleanSchema:
    def test_reduces_framework_output_to_what_every_provider_accepts(self):
        schema = {
            "type": "object", "title": "Args", "additionalProperties": False,
            "$defs": {"X": {}},
            "properties": {
                "action": {"type": "string", "enum": ["list", "create"], "title": "Action"},
                "project_id": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None,
                               "description": "the project"},
                "labels": {"type": ["array", "null"], "items": {"type": "string"}},
            },
            "required": ["action", "not_a_property"],
        }
        out = mc.clean_schema(schema)
        flat = json.dumps(out)
        for banned in ("title", "additionalProperties", "$defs", "default", "anyOf", "null"):
            assert banned not in flat, banned
        assert out["required"] == ["action"]
        assert out["properties"]["project_id"] == {"type": "string", "description": "the project"}
        assert out["properties"]["labels"] == {"type": "array", "items": {"type": "string"}}
        assert out["properties"]["action"]["enum"] == ["list", "create"]


class TestResultToDict:
    def test_text_content_and_error_flag(self):
        ok = SimpleNamespace(content=[SimpleNamespace(type="text", text="hello")],
                             structuredContent=None, isError=False)
        assert mc.result_to_dict("plane", ok) == {"success": True, "connector": "plane",
                                                  "content": "hello"}
        bad = SimpleNamespace(content=[SimpleNamespace(type="text", text="nope")],
                              structuredContent=None, isError=True)
        assert mc.result_to_dict("plane", bad)["error"] == "nope"

    def test_a_huge_result_is_cut(self):
        big = SimpleNamespace(content=[SimpleNamespace(type="text", text="x" * (mc.MAX_RESULT_CHARS + 50))],
                              structuredContent=None, isError=False)
        assert len(mc.result_to_dict("plane", big)["content"]) < mc.MAX_RESULT_CHARS + 100


# ── Registry entries and policy ──────────────────────────────────────────


def _lister(tools):
    return lambda url, headers: tools


class TestBuildEntries:
    def test_write_unless_the_server_says_read(self):
        entries, status = mc.build_entries(
            [_connector()], set(),
            lister=_lister([_tool("get_pql_reference", read_only=True),
                            _tool("workitem", read_only=False),
                            _tool("unannotated")]))
        assert not entries["mcp_plane_get_pql_reference"]["schema"].get("mutating")
        assert entries["mcp_plane_workitem"]["schema"]["mutating"] is True
        assert entries["mcp_plane_unannotated"]["schema"]["mutating"] is True
        assert all(e["schema"]["attended_only"] for e in entries.values())
        assert status["plane"]["read_only"] == ["mcp_plane_get_pql_reference"]

    def test_the_filter_offers_only_what_it_names_and_reports_missing(self):
        entries, status = mc.build_entries(
            [_connector(tools=["workitem", "gone"])], set(),
            lister=_lister([_tool("workitem"), _tool("cycle")]))
        assert set(entries) == {"mcp_plane_workitem"}
        assert status["plane"]["missing"] == ["gone"]

    def test_a_connector_tool_never_shadows_a_native_one(self):
        entries, status = mc.build_entries(
            [_connector(name="x")], {"mcp_x_ssh"}, lister=_lister([_tool("ssh")]))
        assert entries == {} and status["x"]["skipped"] == ["ssh"]

    def test_one_dead_connector_does_not_take_the_others_down(self):
        def lister(url, headers):
            if "dead" in url:
                raise mc.ConnectorError("timed out")
            return [_tool("t")]
        entries, status = mc.build_entries(
            [_connector("dead", url="https://dead.example/mcp"), _connector("live")], set(),
            lister=lister)
        assert set(entries) == {"mcp_live_t"}
        assert status["dead"] == {**status["dead"], "state": "error", "error": "timed out"}

    def test_a_disabled_connector_is_not_listed(self):
        called = []
        entries, status = mc.build_entries(
            [_connector(enabled=False)], set(),
            lister=lambda u, h: called.append(u) or [])
        assert entries == {} and called == [] and status["plane"]["state"] == "disabled"


class TestPolicy:
    """attended_only: connector tools are for a person in a console chat."""

    def _with_connector_tools(self, monkeypatch):
        op, reg = _registry()
        monkeypatch.setattr(mc, "list_remote_tools",
                            lambda url, headers: [_tool("read_it", read_only=True),
                                                  _tool("write_it", read_only=False)])
        reg.refresh_mcp_connectors([_connector()])
        return reg

    def _offered(self, reg, policy):
        return {s["function"]["name"] for s in reg.get_schemas(policy=policy)}

    def test_an_unattended_run_never_sees_one(self, monkeypatch):
        reg = self._with_connector_tools(monkeypatch)
        offered = self._offered(reg, UNATTENDED)
        assert not {n for n in offered if n.startswith("mcp_")}
        out = reg.execute("mcp_plane_read_it", {}, policy=UNATTENDED)
        assert out.get("refused") is True

    def test_an_internal_caller_without_a_policy_is_refused_too(self, monkeypatch):
        # None is "nobody is behind this call", exactly what attended_only refuses.
        reg = self._with_connector_tools(monkeypatch)
        assert reg.execute("mcp_plane_read_it", {}).get("refused") is True

    def test_a_member_gets_the_read_only_ones(self, monkeypatch):
        reg = self._with_connector_tools(monkeypatch)
        offered = self._offered(reg, MEMBER)
        assert "mcp_plane_read_it" in offered and "mcp_plane_write_it" not in offered
        assert reg.execute("mcp_plane_write_it", {}, policy=MEMBER).get("refused") is True

    def test_a_verify_turn_gets_the_read_only_ones(self, monkeypatch):
        reg = self._with_connector_tools(monkeypatch)
        offered = self._offered(reg, VERIFY)
        assert "mcp_plane_read_it" in offered and "mcp_plane_write_it" not in offered

    def test_an_admin_gets_both(self, monkeypatch):
        reg = self._with_connector_tools(monkeypatch)
        assert {"mcp_plane_read_it", "mcp_plane_write_it"} <= self._offered(reg, ADMIN)

    def test_no_marker_reaches_the_model(self, monkeypatch):
        reg = self._with_connector_tools(monkeypatch)
        for schema in reg.get_schemas(policy=ADMIN):
            fn = schema["function"]
            assert "attended_only" not in fn and "mutating" not in fn

    def test_refresh_swaps_and_keeps_native_tools(self, monkeypatch):
        reg = self._with_connector_tools(monkeypatch)
        native = {n for n in reg.tools if not n.startswith("mcp_")}
        reg.refresh_mcp_connectors([])
        assert set(reg.tools) == native
        assert reg.mcp_status == {}


# ── A real server ────────────────────────────────────────────────────────


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    """FastMCP over streamable HTTP, refusing any request without the key."""
    uvicorn = pytest.importorskip("uvicorn")
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    server = FastMCP("cfop-test", stateless_http=True, json_response=True)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_item(identifier: str) -> str:
        """Read one work item by identifier."""
        return f"item {identifier} is Done"

    @server.tool()
    def close_item(identifier: str, note: str = "") -> str:
        """Close a work item."""
        return f"closed {identifier}: {note}"

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def explode() -> str:
        """Always fails."""
        raise RuntimeError("the server broke")

    inner = server.streamable_http_app()

    async def app(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if headers.get(b"x-api-key") != b"right-key":
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"no"})
                return
        await inner(scope, receive, send)

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    assert srv.started, "test MCP server did not start"
    yield f"http://127.0.0.1:{port}/mcp"
    srv.should_exit = True
    thread.join(5)


class TestAgainstARealServer:
    def test_lists_and_calls_through_the_registry(self, live_server, secret):
        entry = mc.parse_input("test", {"url": live_server,
                                        "headers": {"x-api-key": "right-key"}}, None)
        _, reg = _registry()
        status = reg.refresh_mcp_connectors([entry])
        assert status["test"]["state"] == "ok", status
        assert set(status["test"]["tools"]) == {"mcp_test_get_item", "mcp_test_close_item",
                                                "mcp_test_explode"}
        assert reg.is_mutating("mcp_test_close_item")
        assert not reg.is_mutating("mcp_test_get_item")

        out = reg.execute("mcp_test_get_item", {"identifier": "CFOP-438"}, policy=MEMBER)
        assert out == {"success": True, "connector": "test", "content": "item CFOP-438 is Done"}

        out = reg.execute("mcp_test_close_item", {"identifier": "CFOP-438", "note": "moot"},
                          policy=ADMIN)
        assert out["content"] == "closed CFOP-438: moot"

    def test_a_tool_error_comes_back_as_an_error(self, live_server, secret):
        entry = mc.parse_input("test", {"url": live_server,
                                        "headers": {"x-api-key": "right-key"}}, None)
        _, reg = _registry()
        reg.refresh_mcp_connectors([entry])
        out = reg.execute("mcp_test_explode", {}, policy=MEMBER)
        assert "the server broke" in out["error"]

    def test_a_wrong_key_says_so(self, live_server, secret):
        entry = mc.parse_input("test", {"url": live_server,
                                        "headers": {"x-api-key": "wrong"}}, None)
        _, status = mc.build_entries([entry], set())
        assert status["test"]["state"] == "error"
        assert "401" in status["test"]["error"]
        assert "wrong" not in status["test"]["error"]

    def test_an_unreachable_server_fails_fast_and_plainly(self, secret):
        entry = _connector(url=f"http://127.0.0.1:{_free_port()}/mcp")
        started = time.time()
        _, status = mc.build_entries([entry], set())
        assert status["plane"]["state"] == "error"
        assert time.time() - started < mc.LIST_TIMEOUT
