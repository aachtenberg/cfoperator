"""The read-only guard and error translation on the CFOperator API client.

The load-bearing test here is `test_non_get_methods_are_refused`: `attach` is
specified as read-only, and a comment saying so is not a control. Feeding the
transport a POST is the mutation check — remove the guard in
`CFOperatorClient._request` and this test goes red.
"""

import httpx
import pytest

from cfassist.cfoperator import (
    DEFAULT_AGENT_URL,
    ENV_AGENT_URL,
    ENV_API_TOKEN,
    CFOperatorClient,
    CFOperatorError,
    resolve_endpoint,
)


def _client(handler, **kwargs):
    return CFOperatorClient(
        url="http://cfop.test", token="tok",
        transport=httpx.MockTransport(handler), **kwargs
    )


def _json(payload, status=200):
    return lambda request: httpx.Response(status, json=payload)


# ---- read-only guard ------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "get"])
def test_non_get_methods_are_refused(method):
    """Anything that could mutate is rejected before it reaches the network.

    'get' (lowercase) is in the list on purpose: the guard is a membership test
    against an exact-case set, so a caller who writes the method in the wrong
    case must fail loudly rather than slip past a case-insensitive check.
    """
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={})

    with _client(handler) as cf:
        with pytest.raises(CFOperatorError) as exc:
            cf._request(method, "/api/remediations/1/approve")

    assert "read-only" in str(exc.value)
    assert calls == [], "a refused method must not reach the transport"


def test_get_is_allowed():
    with _client(_json({"id": 5})) as cf:
        assert cf._request("GET", "/api/investigations/5") == {"id": 5}


def test_client_exposes_no_mutating_helpers():
    """A structural guard on the class surface, not just the transport.

    Catches the plausible regression where someone adds
    `approve_remediation()` and routes it through a second, unguarded code
    path — the method name would land here before the request guard ever ran.
    """
    forbidden = ("approve", "reject", "resolve", "reclassify", "post", "delete",
                 "update", "create", "queue")
    offenders = [
        name for name in dir(CFOperatorClient)
        if not name.startswith("__") and any(word in name.lower() for word in forbidden)
    ]
    assert offenders == []


# ---- error translation ----------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_explain_the_token(status):
    with _client(_json({"error": "nope"}, status=status)) as cf:
        with pytest.raises(CFOperatorError) as exc:
            cf.get_investigation(1)
    assert "token" in exc.value.message.lower()
    assert ENV_API_TOKEN in (exc.value.hint or "")


def test_not_found_is_reported_as_such():
    with _client(_json({"error": "not found"}, status=404)) as cf:
        with pytest.raises(CFOperatorError) as exc:
            cf.get_investigation(999)
    assert "Not found" in exc.value.message


def test_connect_error_hints_at_the_port_forward():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with _client(handler) as cf:
        with pytest.raises(CFOperatorError) as exc:
            cf.get_investigation(1)
    assert "Cannot reach CFOperator" in exc.value.message
    assert "port-forward" in (exc.value.hint or "")


def test_non_json_response_is_an_error_not_a_crash():
    with _client(lambda r: httpx.Response(200, text="<html>login</html>")) as cf:
        with pytest.raises(CFOperatorError) as exc:
            cf.get_investigation(1)
    assert "non-JSON" in exc.value.message


# ---- reads ----------------------------------------------------------------


def test_remediations_are_filtered_to_the_investigation():
    payload = {"remediations": [
        {"id": 1, "investigation_id": 42},
        {"id": 2, "investigation_id": 7},
        {"id": 3, "investigation_id": None},
        {"id": 4, "investigation_id": 42},
    ]}
    with _client(_json(payload)) as cf:
        rows = cf.remediations_for_investigation(42)
    assert [r["id"] for r in rows] == [1, 4]


def test_search_knowledge_returns_rows_and_mode():
    with _client(_json({"results": [{"id": 9}], "mode": "hybrid"})) as cf:
        rows, mode = cf.search_knowledge("disk full")
    assert rows == [{"id": 9}] and mode == "hybrid"


def test_collect_attach_context_gathers_all_three_reads():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.startswith("/api/investigations/"):
            return httpx.Response(200, json={"id": 42, "trigger": "pod down"})
        if request.url.path == "/api/remediations":
            return httpx.Response(200, json={
                "remediations": [{"id": 3, "investigation_id": 42}]})
        return httpx.Response(200, json={"results": [{"id": 8}], "mode": "fts"})

    with _client(handler) as cf:
        ctx = cf.collect_attach_context(42)

    assert seen == ["/api/investigations/42", "/api/remediations", "/api/kb/search"]
    assert ctx["investigation"]["id"] == 42
    assert [r["id"] for r in ctx["remediations"]] == [3]
    assert ctx["learnings_mode"] == "fts"
    assert ctx["warnings"] == []


def test_enrichment_failures_degrade_to_warnings():
    """A dead KB search must not cost the operator the briefing.

    The investigation is the payload; remediations and learnings are extras.
    Losing an extra during an incident should downgrade the briefing, not
    abort the attach.
    """
    def handler(request):
        if request.url.path.startswith("/api/investigations/"):
            return httpx.Response(200, json={"id": 42, "trigger": "pod down"})
        return httpx.Response(500, json={"error": "boom"})

    with _client(handler) as cf:
        ctx = cf.collect_attach_context(42)

    assert ctx["investigation"]["id"] == 42
    assert ctx["remediations"] == [] and ctx["learnings"] == []
    assert len(ctx["warnings"]) == 2
    assert any("remediation" in w for w in ctx["warnings"])
    assert any("knowledge" in w for w in ctx["warnings"])


def test_missing_investigation_is_fatal():
    with _client(_json({})) as cf:
        with pytest.raises(CFOperatorError):
            cf.collect_attach_context(42)


def test_token_is_sent_as_a_bearer_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"id": 1})

    with _client(handler) as cf:
        cf.get_investigation(1)
    assert seen["auth"] == "Bearer tok"


# ---- endpoint resolution --------------------------------------------------


def test_config_wins_over_environment():
    url, token, timeout = resolve_endpoint(
        {"url": "http://from-config", "token": "cfg-token", "timeout": 5},
        env={ENV_AGENT_URL: "http://from-env", ENV_API_TOKEN: "env-token"},
    )
    assert (url, token, timeout) == ("http://from-config", "cfg-token", 5.0)


def test_environment_fills_in_a_blank_config():
    url, token, _ = resolve_endpoint(
        {"url": "", "token": ""},
        env={ENV_AGENT_URL: "http://from-env/", ENV_API_TOKEN: "env-token"},
    )
    assert (url, token) == ("http://from-env", "env-token")


def test_endpoint_falls_back_to_the_local_default():
    url, token, timeout = resolve_endpoint({}, env={})
    assert (url, token, timeout) == (DEFAULT_AGENT_URL, "", 30.0)


def test_garbage_timeout_does_not_break_resolution():
    _, _, timeout = resolve_endpoint({"timeout": "soon"}, env={})
    assert timeout == 30.0
