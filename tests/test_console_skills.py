"""Guards for the console chat's command lists (CFOP-93).

The chat page has a sidebar of skills and a slash-autocomplete menu. Each
used to carry its own hand-written list; they disagreed with each other and
neither knew about two of the agent's nine skills. Now both render from one
array, and that array comes from the agent (``GET /api/skills``) — the page
holds no list of its own.

These are about wiring, like ``test_console_nav.py``: that the sidebar markup
is empty, that no command literal has crept back into the page, that both
renderers read the same array and that the array is fetched rather than
declared. The node run proves the end-to-end claim — a skill the endpoint
reports and nobody hand-listed appears in both places — and the last test
runs the route on the real ``WebServer``.
"""

from repo_paths import REPO_ROOT
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

ROOT = REPO_ROOT
INDEX = ROOT / "ui" / "index.html"


def html():
    return INDEX.read_text(encoding="utf-8")


def function_source(js, name):
    """The full text of ``function name(...) {...}`` by brace matching."""
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", js)
    assert m, f"index.html has no function {name}"
    start = js.index("{", m.end())
    depth = 0
    for i in range(start, len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[m.start():i + 1]
    raise AssertionError(f"unbalanced braces in {name}")


# --------------------------------------------------------------------------
# source-level: one array, from the agent, feeding both renderers
# --------------------------------------------------------------------------

def test_the_sidebar_markup_carries_no_entries():
    """A hand-written ``<li>`` here is the drift coming back."""
    m = re.search(r'<ul class="skill-list" id="skill-list">(.*?)</ul>', html(), re.S)
    assert m, "the sidebar skill list is gone"
    assert "<li" not in m.group(1), "the sidebar has hand-written entries again"


def test_the_page_declares_no_command_list_of_its_own():
    page = html()
    assert not re.search(r"SLASH_COMMANDS\s*=\s*\[\s*\{", page), \
        "SLASH_COMMANDS is initialised with literal entries"
    assert 'onclick="insertSkill(' not in page, "markup wires its own skill entries"
    assert not re.search(r"\{\s*cmd:\s*'/", page), "a literal command row is back in the page"


def test_both_renderers_read_the_same_array():
    page = html()
    assert "SLASH_COMMANDS" in function_source(page, "renderSkillList")
    assert "SLASH_COMMANDS" in function_source(page, "updateSlashMenu")
    assert "SLASH_COMMANDS" in function_source(page, "handleSlashKeydown")


def test_the_array_is_fetched_from_the_agent():
    page = html()
    loader = function_source(page, "loadSlashCommands")
    assert "fetch('/api/skills')" in loader
    assert re.search(r"SLASH_COMMANDS\s*=", loader), "the loader never fills the array"
    assert "renderSkillList()" in loader, "a fetched list that is never drawn"
    assert re.search(r"^\s*loadSlashCommands\(\);", page, re.M), "nothing calls the loader"


# --------------------------------------------------------------------------
# behaviour, under node: what the endpoint says is what both places show
# --------------------------------------------------------------------------

_STUB = r"""
const mode = process.env.CFOP_TEST_AGENT;
const els = {};
function el(){ return {innerHTML:'', value:'', dataset:{}, listeners:[],
  classList:{_on:false, add(){this._on=true;}, remove(){this._on=false;}, contains(){return this._on;}},
  querySelectorAll(){ return []; }, focus(){} }; }
const document = { getElementById: id => (els[id] = els[id] || el()) };
const inserted = [];
function insertSkill(cmd){ inserted.push(cmd); }
let slashMenuIndex = -1;
const fetch = (url) => {
  if (mode === 'down') return Promise.reject(new Error('agent down'));
  if (url !== '/api/skills') return Promise.reject(new Error('unexpected ' + url));
  return Promise.resolve({json: () => Promise.resolve({skills: [
    {command:'/brand-new-skill', name:'brand-new-skill', args:'<thing>', description:'Only the agent knows this one', kind:'skill'},
    {command:'/sweeps', name:'sweeps', args:'', description:'Recent sweep reports', kind:'shortcut'},
  ]})});
};
%(functions)s
(async () => {
  await loadSlashCommands();
  const sidebar = els['skill-list'].innerHTML;
  document.getElementById('message-input').value = '/brand';
  updateSlashMenu();
  const menu = els['slash-menu'].innerHTML;
  const menuOpen = els['slash-menu'].classList._on;
  document.getElementById('message-input').value = '/sw';
  updateSlashMenu();
  const menuForShortcut = els['slash-menu'].innerHTML;
  console.log(JSON.stringify({sidebar, menu, menuOpen, menuForShortcut, count: SLASH_COMMANDS.length}));
})();
"""


def _run(mode):
    page = html()
    functions = "\n".join(function_source(page, n) for n in
                          ("escapeHtml", "renderSkillList", "loadSlashCommands", "updateSlashMenu"))
    script = _STUB % {"functions": functions}
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30,
                          env={**os.environ, "CFOP_TEST_AGENT": mode})
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@needs_node
def test_a_skill_only_the_agent_knows_reaches_both_places():
    out = _run("up")
    assert out["count"] == 2
    assert "/brand-new-skill" in out["sidebar"]
    assert "Only the agent knows this one" in out["sidebar"]
    assert 'data-cmd="/brand-new-skill"' in out["sidebar"]
    assert out["menuOpen"]
    assert "/brand-new-skill" in out["menu"]
    assert "&lt;thing&gt;" in out["menu"], "args hint is shown, escaped"
    assert "Only the agent knows this one" in out["menu"]
    assert "/sweeps" not in out["menu"], "the filter still narrows to the typed prefix"
    assert "/sweeps" in out["menuForShortcut"]


@needs_node
def test_an_unreachable_agent_shows_no_invented_list():
    out = _run("down")
    assert out["count"] == 0
    assert "unavailable" in out["sidebar"]
    assert "/investigate" not in out["sidebar"]
    assert not out["menuOpen"]


# --------------------------------------------------------------------------
# the route, on the real WebServer
# --------------------------------------------------------------------------

def _client(commands, operator=None):
    from unittest.mock import MagicMock

    from flask import Flask

    from web_auth import install_auth
    from web_server import WebServer

    operator = operator if operator is not None else MagicMock()
    operator.list_slash_commands.return_value = commands

    server = WebServer.__new__(WebServer)
    server.operator = operator
    server.host, server.port = "localhost", 0
    server.app = Flask(__name__)
    server._chat_sessions = {}
    server._sessions_lock = threading.Lock()
    server.auth_store = None
    server._setup_routes()

    prior = {k: os.environ.get(k) for k in
             ("CFOP_AUTH_DISABLED", "CFOP_SESSION_SECRET", "CFOP_UI_USERNAME",
              "CFOP_UI_PASSWORD_HASH", "CFOP_API_TOKEN")}
    os.environ["CFOP_AUTH_DISABLED"] = "1"
    os.environ["CFOP_SESSION_SECRET"] = "test-session-secret"
    for name in ("CFOP_UI_USERNAME", "CFOP_UI_PASSWORD_HASH", "CFOP_API_TOKEN"):
        os.environ[name] = ""
    try:
        install_auth(server.app, ui_dir="ui", store=None)
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return server.app.test_client()


def test_api_skills_returns_what_the_agent_lists():
    rows = [
        {"command": "/brand-new-skill", "name": "brand-new-skill", "args": "<thing>",
         "description": "Only the agent knows this one", "kind": "skill"},
        {"command": "/stats", "name": "stats", "args": "[hours]",
         "description": "Operational summary", "kind": "shortcut"},
    ]
    resp = _client(rows).get("/api/skills")
    assert resp.status_code == 200
    assert resp.get_json() == {"skills": rows}


def test_the_health_poll_refetches_an_empty_skill_list():
    """A console opened while the agent is restarting gets no skills; the
    /api/health poll that already runs is what notices the agent is back, so
    it must refetch rather than leave "Skills unavailable" up until a reload."""
    html = INDEX.read_text(encoding="utf-8")
    start = html.index("function updateStatus()")
    end = html.index("setInterval(updateStatus", start)
    assert "loadSlashCommands()" in html[start:end], "updateStatus never refetches the skill list"


# --------------------------------------------------------------------------
# the composer keeps a draft across navigation (CFOP-113)
# --------------------------------------------------------------------------

def test_the_composer_keeps_a_draft_across_navigation():
    """The header links are real navigations, so a half-typed question used
    to go with the page. Saved on every input event, restored at bootstrap
    (unless a handed row is about to write its own question), cleared only
    once the message is actually sent."""
    h = html()
    assert "cfoperator_chat_draft" in h, "no draft key"
    inp = h[h.index("addEventListener('input'"):]
    inp = inp[:inp.index("});")]
    assert "saveDraft(" in inp, "typing does not save the draft"
    send = h[h.index("function sendMessage("):]
    send = send[:send.index("\n        }\n")]
    assert "saveDraft('')" in send, "sending does not clear the draft"
    boot = h[h.index("const handedInvestigation"):]
    boot = boot[:boot.index("createChatSession(")]
    assert "restoreDraft()" in boot, "the draft is not restored before the session is set up"
    assert "handedRemediation === null && handedInvestigation === null" in boot, (
        "a handed row's question would be overwritten by a stale draft")


# --------------------------------------------------------------------------
# a sweep finding can be handed to the chat (CFOP-115)
# --------------------------------------------------------------------------

def test_a_sweep_finding_can_be_handed_to_the_chat():
    """Each finding row in the sweep panel carries a control that composes a
    question from the finding's own fields — not the row's text — and sends
    it through sendMessage(), the same shape as the drawers' Ask console."""
    h = html()
    row = h[h.index("(r.findings || []).map((f, i) =>"):]
    row = row[:row.index("\n")]
    assert 'onclick="askAboutFinding(${i})"' in row, "no per-finding handoff control"
    assert 'class="ask"' in row and "aria-label=" in row, "the control is not a labelled button"
    q = h[h.index("function findingQuestion("):]
    q = q[:q.index("\n        }\n")]
    for field in ("f.severity", "f.finding", "f.remediation", "f.resolution", "run the checks yourself"):
        assert field in q, f"findingQuestion() no longer carries {field}"
    ask = h[h.index("function askAboutFinding("):]
    ask = ask[:ask.index("\n        }\n")]
    assert "findingQuestion(SWEEP, i)" in ask and "sendMessage({mode: 'verify'});" in ask
    assert "saveDraft(draft)" in ask, "a composer draft is lost by the handoff"
    # The behaviours the change is about, pinned (CFOP-115 review): the
    # click waits while a send is out or an answer streams, and folds the
    # panel afterwards; the poll keeps SWEEP current without folding an
    # open panel under the operator.
    assert "if (inFlightChatId || SENDING)" in ask, "the click does not honour the send lock"
    assert "classList.remove('open')" in ask, "the panel is not folded after the handoff"
    load = h[h.index("function loadSweepReports("):]
    load = load[:load.index("\n        }\n")]
    assert "SWEEP = r;" in load, "the latest report is not kept on the page"
    assert "wasOpen" in load and "classList.add('open')" in load, "the 60 s repaint folds an open panel"
    assert "(r.findings || []).map" in load, "a report without findings throws mid-repaint"


def test_sending_is_locked_from_the_first_call_not_from_the_reply():
    """inFlightChatId exists only once /api/chat has answered; before the
    CFOP-115 review, a second Enter (or a finding click) in that window fired
    a second POST — the disabled Send button was the only lock and Enter
    never looked at it. SENDING is set synchronously on the first call and
    cleared on both outcomes of the POST."""
    h = html()
    send = h[h.index("function sendMessage("):]
    send = send[:send.index("\n        }\n")]
    assert "if (inFlightChatId || SENDING) return;" in send
    assert "SENDING = true;" in send
    assert send.count("SENDING = false;") == 2, "the lock is not released on both outcomes"



# --------------------------------------------------------------------------
# hand-offs are verification passes, and the route knows who is asking (CFOP-124)
# --------------------------------------------------------------------------

def test_every_handoff_is_a_verification_pass():
    """The three question builders share one VERIFY_ONLY clause and the three
    hand-offs send mode: 'verify', which the agent turns into a tool policy
    that withholds mutating tools for that turn. Before this, gemini ran a
    sweep's "Proposed remediation: sudo systemctl restart …" under a "check
    whether this is still the case" ask (session 23, 2026-08-28)."""
    h = html()
    const = re.search(r"const VERIFY_ONLY = (.*?);\n", h, re.S)
    assert const, "index.html has no VERIFY_ONLY clause"
    assert "do not restart" in const.group(1)
    for builder in ("remediationQuestion", "investigationQuestion", "findingQuestion"):
        src = function_source(h, builder)
        assert "VERIFY_ONLY" in src, f"{builder}() does not carry the verify-only clause"
        assert "run the checks yourself" in src, f"{builder}() no longer asks for the checks to be run"
    for ask in ("askAboutRemediation", "askAboutInvestigation", "askAboutFinding"):
        src = function_source(h, ask)
        assert "sendMessage({mode: 'verify'})" in src, f"{ask}() does not send mode: 'verify'"
    send = function_source(h, "sendMessage")
    assert "payload.mode = opts.mode" in send, "sendMessage() drops the mode"


def test_api_chat_resolves_the_role_in_the_route_and_threads_it(monkeypatch):
    """The role is resolved in the route — the only place request context
    exists; the chat runs in a thread — and reaches the agent with the verify
    flag. An unknown identity is a member: None would mean "internal caller"
    and lift every restriction."""
    from unittest.mock import MagicMock

    import web_server

    operator = MagicMock()

    def stream(*args, **kwargs):
        yield {"event": "done", "data": {"response": "ok", "backend": "x", "model": "y", "tool_calls": 0}}
    operator.handle_chat_message_stream.side_effect = stream
    c = _client([], operator=operator)

    def ask(role, body):
        monkeypatch.setattr(web_server, "_effective_role", lambda: role)
        resp = c.post("/api/chat", json=body)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        chat_id = resp.get_json()["chat_id"]
        for _ in range(100):
            if c.get(f"/api/chat/events/{chat_id}").get_json().get("done"):
                break
            time.sleep(0.02)
        return operator.handle_chat_message_stream.call_args.kwargs

    assert ask("admin", {"message": "hi"}) == {"model": None, "actor_role": "admin", "verify_only": False}
    kw = ask("member", {"message": "hi", "mode": "verify"})
    assert kw["actor_role"] == "member" and kw["verify_only"] is True
    assert ask(None, {"message": "hi"})["actor_role"] == "member"
