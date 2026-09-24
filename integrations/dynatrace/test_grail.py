"""Grail query client (CFOP-203).

The response bodies below are trimmed from real answers of a Dynatrace SaaS
tenant, captured 2026-09-24 with a platform token. Only request tokens and
query ids are replaced.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from integrations.dynatrace import grail
from integrations.dynatrace.grail import GrailClient, GrailQueryError

URL = "https://abc12345.apps.dynatrace.com"
TOKEN = "dt0s16.TESTTOKEN.SECRETPART"

SUCCEEDED = {
    "state": "SUCCEEDED",
    "progress": 100,
    "result": {
        "records": [{"entity.name": "ubuntu-itx-01"}],
        "types": [{"indexRange": [0, 0], "mappings": {"entity.name": {"type": "string"}}}],
        "metadata": {"grail": {"queryId": "q-1", "scannedRecords": "1", "notifications": []}},
    },
}
RUNNING = {"state": "RUNNING", "requestToken": "tok/with+chars==", "ttlSeconds": 399}
LIMITED = {
    "state": "SUCCEEDED",
    "progress": 100,
    "result": {
        "records": [{"content": "a"}, {"content": "b"}],
        "types": [],
        "metadata": {"grail": {"notifications": [{
            "arguments": ["2"],
            "message": "Your result has been limited to 2.",
            "notificationType": "API_RECORDS_LIMIT_ADDED",
            "severity": "WARNING",
        }]}},
    },
}
EMPTY = {"state": "SUCCEEDED", "progress": 100, "result": {"records": [], "types": [], "metadata": {"grail": {}}}}
SYNTAX_ERROR = {"error": {"message": "UNKNOWN_COMMAND", "code": 400, "details": {
    "exceptionType": "DQL-SYNTAX-ERROR",
    "errorType": "UNKNOWN_COMMAND",
    "errorMessage": "There's no command `limt`.",
    "queryString": "fetch logs | limt 5",
    "queryId": "q-2",
    "syntaxErrorPosition": {"start": {"column": 14, "index": 13, "line": 1},
                            "end": {"column": 17, "index": 16, "line": 1}},
}}}
BAD_TOKEN = {"error": {"code": 401, "message": "Platform token has invalid format.", "details": {"traceId": "t"}}}
GONE = {"error": {"message": "QUERY_GONE", "code": 410, "details": {
    "exceptionType": "GONE",
    "errorType": "QUERY_GONE",
    "errorMessage": "The query for this query ID is not available anymore.",
}}}


class FakeGrail:
    """Stands in for urlopen: answers from a script, records every request."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request, timeout=None):
        body = json.loads(request.data.decode()) if request.data else None
        self.requests.append({
            "method": request.get_method(),
            "url": request.full_url,
            "headers": {k.lower(): v for k, v in request.header_items()},
            "body": body,
            "timeout": timeout,
        })
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, payload = answer
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        if status >= 400:
            raise HTTPError(request.full_url, status, "error", {}, io.BytesIO(raw.encode()))
        return _Response(status, raw)


class _Response(io.BytesIO):
    def __init__(self, status, raw):
        super().__init__(raw.encode())
        self.status = status


@pytest.fixture
def fake(monkeypatch):
    def install(*answers):
        fake_grail = FakeGrail(*answers)
        monkeypatch.setattr(grail, "urlopen", fake_grail)
        return fake_grail
    return install


def test_an_inline_answer_is_returned_without_polling(fake):
    transport = fake((200, SUCCEEDED))
    result = GrailClient(URL, TOKEN).query("fetch dt.entity.host | fields entity.name")

    assert result.records == [{"entity.name": "ubuntu-itx-01"}]
    assert result.metadata["grail"]["queryId"] == "q-1"
    assert result.warnings == []
    (req,) = transport.requests
    assert req["method"] == "POST"
    assert req["url"] == f"{URL}/platform/storage/query/v1/query:execute"
    assert req["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert req["body"]["query"] == "fetch dt.entity.host | fields entity.name"
    assert req["body"]["maxResultRecords"] == 1000
    assert 1 <= req["body"]["requestTimeoutMilliseconds"] <= 10_000


def test_a_running_query_is_polled_with_its_request_token(fake):
    transport = fake((202, RUNNING), (200, SUCCEEDED))
    result = GrailClient(URL, TOKEN).query("fetch logs | summarize n=count()")

    assert result.records == [{"entity.name": "ubuntu-itx-01"}]
    poll = transport.requests[1]
    assert poll["method"] == "GET" and poll["body"] is None
    parts = urlsplit(poll["url"])
    assert parts.path == "/platform/storage/query/v1/query:poll"
    query = parse_qs(parts.query)
    assert query["request-token"] == ["tok/with+chars=="]   # quoted on the wire, intact after parsing
    assert 1 <= int(query["request-timeout-milliseconds"][0]) <= 10_000


def test_a_query_past_its_deadline_is_cancelled_not_abandoned(fake):
    transport = fake((202, RUNNING), (200, SUCCEEDED))  # Grail answers cancel with whatever it has
    with pytest.raises(GrailQueryError, match="did not finish within 0s and was cancelled"):
        GrailClient(URL, TOKEN, timeout=0).query("fetch logs")
    cancel = transport.requests[1]
    assert cancel["method"] == "POST"
    assert urlsplit(cancel["url"]).path == "/platform/storage/query/v1/query:cancel"


def test_a_failed_state_is_an_error(fake):
    fake((202, RUNNING), (200, {"state": "FAILED"}))
    with pytest.raises(GrailQueryError, match="state FAILED"):
        GrailClient(URL, TOKEN).query("fetch logs")


def test_a_syntax_error_carries_grails_message_and_position(fake):
    fake((400, SYNTAX_ERROR))
    with pytest.raises(GrailQueryError) as info:
        GrailClient(URL, TOKEN).query("fetch logs | limt 5")
    assert str(info.value) == "HTTP 400 DQL-SYNTAX-ERROR: There's no command `limt`. (line 1, column 14)"
    assert info.value.status == 400
    assert info.value.error_type == "DQL-SYNTAX-ERROR"
    assert info.value.query_id == "q-2"


def test_an_auth_failure_says_what_grail_said(fake):
    fake((401, BAD_TOKEN))
    with pytest.raises(GrailQueryError, match=r"^HTTP 401: Platform token has invalid format\.$"):
        GrailClient(URL, TOKEN).query("fetch logs")


def test_a_consumed_request_token_surfaces_as_gone(fake):
    fake((202, RUNNING), (410, GONE))
    with pytest.raises(GrailQueryError, match="GONE: The query for this query ID is not available anymore"):
        GrailClient(URL, TOKEN).query("fetch logs")


def test_a_non_json_error_page_is_reported_not_parsed(fake):
    fake((503, "<html><body><h1>503 Service Unavailable</h1></body></html>"))
    with pytest.raises(GrailQueryError, match="HTTP 503: <html><body><h1>503 Service Unavailable"):
        GrailClient(URL, TOKEN).query("fetch logs")


def test_a_network_failure_is_an_error_not_an_empty_result(fake):
    fake(URLError(ConnectionResetError(104, "Connection reset by peer")))
    with pytest.raises(GrailQueryError, match="cannot reach https://abc12345.apps.dynatrace.com"):
        GrailClient(URL, TOKEN).query("fetch logs")


def test_an_empty_result_is_a_result(fake):
    fake((200, EMPTY))
    assert GrailClient(URL, TOKEN).query("fetch logs | filter false").records == []


def test_a_limited_result_says_so(fake):
    transport = fake((200, LIMITED))
    result = GrailClient(URL, TOKEN).query("fetch logs | limit 50", max_records=2)
    assert transport.requests[0]["body"]["maxResultRecords"] == 2
    assert len(result.records) == 2
    assert result.warnings == ["Your result has been limited to 2."]


def test_an_explicit_zero_limit_is_sent_not_replaced_by_the_default(fake):
    transport = fake((200, EMPTY))
    GrailClient(URL, TOKEN).query("fetch logs", max_records=0)
    assert transport.requests[0]["body"]["maxResultRecords"] == 0


def test_the_timeframe_is_sent_in_utc(fake):
    transport = fake((200, EMPTY))
    GrailClient(URL, TOKEN).query(
        "fetch logs",
        timeframe_start=datetime(2026, 9, 24, 16, 0),                       # naive: taken as UTC
        timeframe_end=datetime(2026, 9, 24, 18, 5, tzinfo=timezone.utc),
    )
    body = transport.requests[0]["body"]
    assert body["defaultTimeframeStart"] == "2026-09-24T16:00:00Z"
    assert body["defaultTimeframeEnd"] == "2026-09-24T18:05:00Z"


@pytest.mark.parametrize("url", ["", "abc12345.apps.dynatrace.com", "ftp://abc12345.apps.dynatrace.com"])
def test_a_url_that_is_not_absolute_is_refused(url):
    with pytest.raises(ValueError, match="must be absolute"):
        GrailClient(url, TOKEN)


def test_the_classic_live_host_is_refused_with_the_right_one_named():
    with pytest.raises(ValueError, match=r"platform host \(https://abc12345\.apps\.dynatrace\.com\)"):
        GrailClient("https://abc12345.live.dynatrace.com", TOKEN)


@pytest.mark.parametrize("token", ["", "   ", None])
def test_an_empty_token_is_refused(token):
    with pytest.raises(ValueError, match="token is empty"):
        GrailClient(URL, token)


def test_the_token_stays_out_of_repr_and_the_url_is_normalised():
    client = GrailClient(URL + "/", TOKEN)
    assert client.url == URL
    assert "SECRETPART" not in repr(client)
