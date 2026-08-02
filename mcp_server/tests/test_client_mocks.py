"""CfopClient against a mocked httpx transport: routing, params, error mapping."""

import asyncio
import json

import httpx
import pytest

from mcp_server.client import CfopClient, UpstreamError
from mcp_server.config import Settings


def make_client(handler):
    return CfopClient(Settings(), transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


def test_triage_posts_alert_and_returns_decision():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"action": "monitor", "confidence": 0.8})

    client = make_client(handler)
    decision = run(client.triage({"summary": "pod down", "severity": "warning"}))
    assert decision["action"] == "monitor"
    assert seen == {
        "method": "POST",
        "path": "/v1/triage",
        "body": {"summary": "pod down", "severity": "warning"},
    }


def test_list_remediations_passes_status_and_limit():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"remediations": [], "count": 0})

    client = make_client(handler)
    run(client.list_remediations(status="queued", limit=10))
    assert seen["params"] == {"status": "queued", "limit": "10"}

    run(client.list_remediations())
    assert seen["params"] == {"limit": "50"}


def test_reject_posts_note_body():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 7, "status": "rejected"})

    client = make_client(handler)
    run(client.reject_remediation(7, note="won't fix"))
    assert seen["path"] == "/api/remediations/7/reject"
    assert seen["body"] == {"note": "won't fix"}


def test_resolve_posts_note_body():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 7, "status": "resolved"})

    client = make_client(handler)
    run(client.resolve_remediation(7, note="fixed by hand"))
    assert seen["path"] == "/api/remediations/7/resolve"
    assert seen["body"] == {"note": "fixed by hand"}

    run(client.resolve_remediation(7))
    assert seen["body"] == {}


def test_404_maps_to_not_found_not_retryable():
    client = make_client(
        lambda request: httpx.Response(404, json={"error": "not found"}))
    with pytest.raises(UpstreamError) as exc:
        run(client.get_investigation(999))
    assert exc.value.code == "not_found"
    assert exc.value.retryable is False
    assert exc.value.message == "not found"


def test_400_maps_to_validation():
    client = make_client(
        lambda request: httpx.Response(400, json={"error": "Field summary is required"}))
    with pytest.raises(UpstreamError) as exc:
        run(client.start_investigation({}))
    assert exc.value.code == "validation"
    assert "summary" in exc.value.message


def test_503_maps_to_retryable_upstream_unavailable():
    client = make_client(
        lambda request: httpx.Response(503, json={"error": "investigation queue full"}))
    with pytest.raises(UpstreamError) as exc:
        run(client.start_investigation({"summary": "x"}))
    assert exc.value.code == "upstream_unavailable"
    assert exc.value.retryable is True


def test_connect_error_maps_to_retryable_upstream_unavailable():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    client = make_client(handler)
    with pytest.raises(UpstreamError) as exc:
        run(client.list_investigations())
    assert exc.value.code == "upstream_unavailable"
    assert exc.value.retryable is True


def test_non_json_success_body_is_an_error():
    client = make_client(lambda request: httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(UpstreamError) as exc:
        run(client.list_investigations())
    assert exc.value.code == "upstream_unavailable"
    assert exc.value.retryable is False
