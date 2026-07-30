"""Bridge unit tests: runtime turns/history, event filtering, chunking,
and the Slack post path — all with stubs, no real Slack or agent."""

import asyncio

from bridge.config import BridgeSettings
from bridge.runtimes.local_cfop import LocalCfopRuntime
from bridge.slack_app import SlackBridge, chunk_text

import pytest


class StubClient:
    def __init__(self):
        self.chats = []
        self.investigations = []
        self.investigate_status = "queued"

    async def run_chat(self, message, history=None, timeout=None,
                       backend="auto", model=None):
        self.chats.append({"message": message, "history": list(history or []),
                           "backend": backend, "model": model})
        return {"chat_id": "c1", "response": f"reply to: {message}", "tool_calls": 2}

    async def start_investigation(self, alert):
        self.investigations.append(alert)
        return {"status": self.investigate_status, "queue_depth": 1,
                "alert_id": alert.get("alert_id")}


def make_bridge(runtime=None, web=None):
    settings = BridgeSettings(slack_bot_token="xoxb-t", slack_app_token="xapp-t")
    return SlackBridge(settings, runtime, web_client=web)


class FakeWeb:
    def __init__(self):
        self.posts = []
        self.reactions = []

    def chat_postMessage(self, channel, thread_ts, text):
        self.posts.append({"channel": channel, "thread_ts": thread_ts, "text": text})

    def reactions_add(self, channel, timestamp, name):
        self.reactions.append(name)


# --- LocalCfopRuntime ---

def test_runtime_threads_history_and_caps_it():
    stub = StubClient()
    rt = LocalCfopRuntime(stub, max_history_turns=2)

    r1 = asyncio.run(rt.start("how is pi5?", thread_id="t1"))
    assert r1 == "reply to: how is pi5?"
    assert stub.chats[0]["history"] == []

    asyncio.run(rt.follow_up("t1", "and pi3?"))
    assert stub.chats[1]["history"] == [
        {"role": "user", "content": "how is pi5?"},
        {"role": "assistant", "content": "reply to: how is pi5?"},
    ]

    asyncio.run(rt.follow_up("t1", "third"))
    asyncio.run(rt.follow_up("t1", "fourth"))
    # cap = 2 turns = 4 messages
    assert len(rt._history["t1"]) == 4
    assert rt._history["t1"][0]["content"] == "third"


def test_runtime_threads_are_isolated():
    stub = StubClient()
    rt = LocalCfopRuntime(stub)
    asyncio.run(rt.start("q1", thread_id="t1"))
    asyncio.run(rt.start("q2", thread_id="t2"))
    assert stub.chats[1]["history"] == []


def test_investigate_prefix_enqueues_with_idempotency_key():
    stub = StubClient()
    rt = LocalCfopRuntime(stub)
    reply = asyncio.run(rt.start("investigate: pod X crashlooping", thread_id="t9"))
    assert "enqueued" in reply
    alert = stub.investigations[0]
    assert alert["summary"] == "pod X crashlooping"
    assert alert["source"] == "slack-bridge"
    assert alert["idempotency_key"].startswith("slack-t9-")

    stub.investigate_status = "deduped"
    reply2 = asyncio.run(rt.start("investigate: pod X crashlooping", thread_id="t9"))
    assert "deduped" in reply2.lower()


def test_claude_prefix_escalates_one_turn_to_anthropic():
    stub = StubClient()
    rt = LocalCfopRuntime(stub, escalation_model="claude-opus-5")

    asyncio.run(rt.start("how is cm5?", thread_id="t1"))
    assert stub.chats[0]["backend"] == "auto" and stub.chats[0]["model"] is None

    reply = asyncio.run(rt.follow_up("t1", "claude: is that etcd diagnosis real?"))
    assert stub.chats[1]["backend"] == "anthropic"
    assert stub.chats[1]["model"] == "claude-opus-5"
    assert stub.chats[1]["message"] == "is that etcd diagnosis real?"
    assert reply.startswith(":brain:")
    # prior local-model turn stays in history for the escalated turn
    assert stub.chats[1]["history"][0]["content"] == "how is cm5?"

    # next un-prefixed turn drops back to the local model
    asyncio.run(rt.follow_up("t1", "ok thanks"))
    assert stub.chats[2]["backend"] == "auto"

    # opus: alias works too
    asyncio.run(rt.start("Opus: second opinion", thread_id="t2"))
    assert stub.chats[3]["backend"] == "anthropic"
    assert stub.chats[3]["message"] == "second opinion"


def test_investigate_prefix_without_body_is_usage_hint():
    stub = StubClient()
    rt = LocalCfopRuntime(stub)
    reply = asyncio.run(rt.start("investigate:", thread_id="t1"))
    assert reply.startswith("Usage:")
    assert stub.investigations == []


# --- event filtering ---

def test_should_handle_filters_bots_subtypes_and_channels():
    b = make_bridge()
    assert b.should_handle({"type": "app_mention", "text": "hi"})
    assert b.should_handle({"type": "message", "channel_type": "im"})
    assert not b.should_handle({"type": "message", "channel_type": "channel"})
    assert not b.should_handle({"type": "message", "channel_type": "im",
                                "bot_id": "B1"})
    assert not b.should_handle({"type": "message", "channel_type": "im",
                                "subtype": "message_changed"})


def test_event_dedupe():
    b = make_bridge()
    assert b._dedupe("Ev1") is False
    assert b._dedupe("Ev1") is True
    assert b._dedupe(None) is False
    assert b._dedupe(None) is False


def test_strip_mention():
    b = make_bridge()
    assert b.strip_mention("<@U12345> how is pi5?") == "how is pi5?"
    assert b.strip_mention("no mention") == "no mention"


# --- turn processing ---

class EchoRuntime:
    def __init__(self):
        self.calls = []

    async def start(self, prompt, *, thread_id):
        self.calls.append(("start", thread_id, prompt))
        return f"started: {prompt}"

    async def follow_up(self, thread_id, prompt):
        self.calls.append(("follow_up", thread_id, prompt))
        return f"followed: {prompt}"


def test_process_event_start_then_follow_up_on_thread():
    rt, web = EchoRuntime(), FakeWeb()
    b = make_bridge(runtime=rt, web=web)

    b.process_event({"type": "app_mention", "channel": "C1", "ts": "100.1",
                     "text": "<@U1> how is pi5?"})
    b.process_event({"type": "app_mention", "channel": "C1", "ts": "100.2",
                     "thread_ts": "100.1", "text": "<@U1> and pi3?"})

    assert rt.calls == [("start", "100.1", "how is pi5?"),
                        ("follow_up", "100.1", "and pi3?")]
    assert [p["thread_ts"] for p in web.posts] == ["100.1", "100.1"]
    assert web.reactions == ["eyes", "eyes"]


def test_process_event_runtime_error_is_reported_not_raised():
    class BoomRuntime:
        async def start(self, prompt, *, thread_id):
            raise RuntimeError("agent down")

    web = FakeWeb()
    b = make_bridge(runtime=BoomRuntime(), web=web)
    b.process_event({"type": "app_mention", "channel": "C1", "ts": "1.1",
                     "text": "<@U1> hi"})
    assert "agent down" in web.posts[0]["text"]


def test_empty_prompt_gets_usage_message():
    web = FakeWeb()
    b = make_bridge(runtime=EchoRuntime(), web=web)
    b.process_event({"type": "app_mention", "channel": "C1", "ts": "1.1",
                     "text": "<@U1>"})
    assert "Ask me something" in web.posts[0]["text"]


def test_consecutive_turns_share_one_event_loop():
    """Regression: asyncio.run() per turn closed the loop the shared httpx
    client had bound its pool to — turn 1 worked, turn 2 raised
    'Event loop is closed'. Drive two turns through the SAME bridge with a
    real CfopClient over a mock transport."""
    import httpx

    from mcp_server.client import CfopClient
    from mcp_server.config import Settings

    def handler(request):
        if request.url.path == "/api/chat":
            return httpx.Response(200, json={"chat_id": "c9"})
        return httpx.Response(200, json={
            "events": [{"event": "done", "data": {"response": "pong"}}],
            "cursor": 1, "done": True,
        })

    client = CfopClient(Settings(chat_poll_interval=0.01),
                        transport=httpx.MockTransport(handler))
    rt = LocalCfopRuntime(client)
    web = FakeWeb()
    b = make_bridge(runtime=rt, web=web)

    b.process_event({"type": "app_mention", "channel": "C1", "ts": "1.1",
                     "text": "<@U1> ping one"})
    b.process_event({"type": "app_mention", "channel": "C1", "ts": "2.1",
                     "text": "<@U1> ping two"})

    texts = [p["text"] for p in web.posts]
    assert texts == ["pong", "pong"], f"second turn failed: {texts}"


# --- chunking ---

def test_chunk_text_respects_limit_and_reassembles():
    text = "\n".join(f"line {i} " + "x" * 80 for i in range(100))
    chunks = chunk_text(text, 500)
    assert all(len(c) <= 500 for c in chunks)
    assert "".join(chunks) == text


def test_chunk_text_hard_splits_monster_lines():
    text = "y" * 1200
    chunks = chunk_text(text, 500)
    assert [len(c) for c in chunks] == [500, 500, 200]


# --- config ---

def test_bridge_settings_require_both_tokens():
    with pytest.raises(ValueError):
        BridgeSettings.from_env({"SLACK_BOT_TOKEN": "xoxb-1"})
    s = BridgeSettings.from_env(
        {"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_APP_TOKEN": "xapp-1"})
    assert s.runtime == "local"


# --- AnthropicRuntime ---

BASE_ENV = {"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_APP_TOKEN": "xapp-1"}


def test_anthropic_runtime_requires_mcp_token_and_api_key():
    env = dict(BASE_ENV, CFOP_BRIDGE_RUNTIME="anthropic")
    with pytest.raises(ValueError, match="CFOP_MCP_TOKEN"):
        BridgeSettings.from_env(env)
    env["CFOP_MCP_TOKEN"] = "t"
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        BridgeSettings.from_env(env)
    env["ANTHROPIC_API_KEY"] = "sk-ant-x"
    s = BridgeSettings.from_env(env)
    assert s.runtime == "anthropic"
    assert s.anthropic_model == "claude-opus-5"


def test_anthropic_runtime_threads_history():
    from bridge.runtimes.anthropic import AnthropicRuntime

    settings = BridgeSettings(
        slack_bot_token="x", slack_app_token="x", runtime="anthropic",
        mcp_token="t", max_history_turns=2)
    rt = AnthropicRuntime(settings, anthropic_client=object())
    seen = []

    async def fake_loop(messages):
        seen.append(list(messages))
        return f"reply {len(seen)}"

    rt._run_agent_loop = fake_loop
    r1 = asyncio.run(rt.start("q1", thread_id="t1"))
    assert r1 == "reply 1"
    asyncio.run(rt.follow_up("t1", "q2"))
    assert seen[1] == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "reply 1"},
        {"role": "user", "content": "q2"},
    ]
    # cap: 2 turns = 4 messages
    asyncio.run(rt.follow_up("t1", "q3"))
    asyncio.run(rt.follow_up("t1", "q4"))
    assert len(rt._history["t1"]) == 4


def test_reply_from_message_handles_refusal_and_text():
    from types import SimpleNamespace

    from bridge.runtimes.anthropic import _reply_from_message

    refusal = SimpleNamespace(stop_reason="refusal", content=[])
    assert "declined" in _reply_from_message(refusal)

    msg = SimpleNamespace(stop_reason="end_turn", content=[
        SimpleNamespace(type="thinking", thinking=""),
        SimpleNamespace(type="text", text="all good "),
        SimpleNamespace(type="text", text="on the fleet"),
    ])
    assert _reply_from_message(msg) == "all good on the fleet"

    empty = SimpleNamespace(stop_reason="end_turn", content=[])
    assert "no text" in _reply_from_message(empty)
    with pytest.raises(RuntimeError):
        _reply_from_message(None)
