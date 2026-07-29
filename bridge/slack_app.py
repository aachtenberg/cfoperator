"""Slack Socket Mode app: mentions/DMs in, threaded replies out.

Socket Mode is an outbound websocket — the homelab exposes nothing. Events
are acked immediately (Slack retries un-acked deliveries in ~3s, far shorter
than an agent chat) and processed on worker threads; replies land on the
message's thread.
"""

import asyncio
import logging
import re
import threading
from collections import OrderedDict

logger = logging.getLogger(__name__)

SLACK_MAX_CHUNK = 3900  # Slack hard limit is 4000 chars per message
MENTION_RE = re.compile(r"<@[A-Z0-9]+>")


class SlackBridge:
    def __init__(self, settings, runtime, web_client=None, socket_client=None):
        self._settings = settings
        self._runtime = runtime
        # Injectable for tests; real clients built lazily in run().
        self._web = web_client
        self._socket = socket_client
        self._bot_user_id = None
        self._seen_events = OrderedDict()  # event_id -> None, LRU
        self._known_threads = set()
        self._threads_lock = threading.Lock()

    # --- event filtering ---

    def _dedupe(self, event_id):
        """True if this event_id was already handled (Slack redelivers)."""
        if not event_id:
            return False
        if event_id in self._seen_events:
            return True
        self._seen_events[event_id] = None
        while len(self._seen_events) > 1000:
            self._seen_events.popitem(last=False)
        return False

    def should_handle(self, event):
        if event.get("bot_id") or event.get("subtype"):
            return False
        etype = event.get("type")
        if etype == "app_mention":
            return True
        # DMs arrive as plain messages; channel messages without a mention
        # are not ours to answer.
        return etype == "message" and event.get("channel_type") == "im"

    def strip_mention(self, text):
        return MENTION_RE.sub("", text or "").strip()

    # --- turn processing ---

    def process_event(self, event):
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        prompt = self.strip_mention(event.get("text"))
        if not prompt:
            self._post(channel, thread_ts,
                       "Ask me something about the fleet — e.g. "
                       "`how is raspberrypi5?` or `investigate: pod X is crashlooping`.")
            return

        with self._threads_lock:
            is_new = thread_ts not in self._known_threads
            self._known_threads.add(thread_ts)

        self._react(channel, event.get("ts"), "eyes")
        try:
            if is_new:
                reply = asyncio.run(
                    self._runtime.start(prompt, thread_id=thread_ts))
            else:
                reply = asyncio.run(
                    self._runtime.follow_up(thread_ts, prompt))
        except Exception as e:
            logger.error("turn failed in thread %s: %s", thread_ts, e, exc_info=True)
            reply = f":warning: That didn't work: {e}"
        for chunk in chunk_text(reply, SLACK_MAX_CHUNK):
            self._post(channel, thread_ts, chunk)

    # --- slack plumbing ---

    def _post(self, channel, thread_ts, text):
        try:
            self._web.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
        except Exception as e:
            logger.error("chat_postMessage failed: %s", e)

    def _react(self, channel, ts, name):
        try:
            self._web.reactions_add(channel=channel, timestamp=ts, name=name)
        except Exception:
            pass  # cosmetic; already_reacted etc. are fine

    def handle_socket_request(self, client, req):
        from slack_sdk.socket_mode.response import SocketModeResponse
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "events_api":
            return
        if self._dedupe(req.payload.get("event_id")):
            return
        event = req.payload.get("event") or {}
        if not self.should_handle(event):
            return
        threading.Thread(
            target=self.process_event, args=(event,),
            daemon=True, name=f"bridge-turn-{event.get('ts', '?')}",
        ).start()

    def run(self):
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.web import WebClient

        if self._web is None:
            self._web = WebClient(token=self._settings.slack_bot_token)
        self._bot_user_id = self._web.auth_test()["user_id"]
        if self._socket is None:
            self._socket = SocketModeClient(
                app_token=self._settings.slack_app_token,
                web_client=self._web,
            )
        self._socket.socket_mode_request_listeners.append(self.handle_socket_request)
        self._socket.connect()
        logger.info("Slack bridge connected (bot user %s, runtime %s)",
                    self._bot_user_id, self._settings.runtime)
        threading.Event().wait()


def chunk_text(text, size):
    """Split on line boundaries where possible, hard-split otherwise."""
    if len(text) <= size:
        return [text]
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > size:
            chunks.append(line[:size])
            line = line[size:]
        if len(current) + len(line) > size:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks
