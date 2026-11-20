"""Admin → LLM draws the flow and puts the model picker on each step (CFOP-737).

The page is only worth having if what it draws is what the agent does. So
these guards are about agreement, not about today's markup:

- the chain the page draws is built from the constant the provider walk uses,
  and for every selection tried here its usable rungs ARE the walk, in order,
  with the same models. A page with its own copy of the order would go on
  drawing it after the agent stopped using it;
- a rung the walk would leave out is still drawn, with the reason;
- choosing a rung's model does not move that rung to the front;
- every step the server lists has a place in the page's layout, so a scope
  added later fails here until someone decides where it sits (at run time it
  would land under "Other", which is a fallback, not a decision).
"""

from repo_paths import REPO_ROOT
import os
import re
import sys
from types import MethodType, SimpleNamespace

import pytest

sys.path.insert(0, str(REPO_ROOT))
sys.path.append(os.path.join(str(REPO_ROOT), "agent"))

from test_console_provider_registry import server  # noqa: E402,F401 -- the fixture
from agent import CFOperator, LLM_FALLBACK_ORDER, LLM_SCOPES, OPENAI_COMPAT_PROVIDERS  # noqa: E402

ADMIN = REPO_ROOT / "ui" / "admin.html"
HOSTED_KEYS = ["ANTHROPIC_API_KEY"] + [cfg["key_env"] for cfg in OPENAI_COMPAT_PROVIDERS.values()]


class _KB:
    def __init__(self, **settings):
        self.d = dict(settings)

    def get_setting(self, key, default="", **_kw):
        return self.d.get(key, default)

    def set_setting(self, key, value):
        self.d[key] = value


def _operator(**settings):
    """A real CFOperator with no __init__: the real walk and the real view
    over a dict of settings and a config that names a model for every rung."""
    op = CFOperator.__new__(CFOperator)
    op.kb = _KB(**settings)
    op.config = {"llm": {
        "primary": {"url": "http://ollama:11434", "model": "local-m"},
        "fallback": [{"provider": "groq", "model": "groq-m"}, {"provider": "xai", "model": "xai-m"},
                     {"provider": "gemini", "model": "gemini-m"}, {"provider": "anthropic", "model": "claude-m"}],
    }}
    op.llm = SimpleNamespace(get_next_provider=lambda: ("ollama", "http://ollama:11434", "local-m"))
    return op


@pytest.fixture
def keys(monkeypatch):
    """Every hosted key set; a test removes the ones it wants missing."""
    for env in HOSTED_KEYS:
        monkeypatch.setenv(env, "k")
    return monkeypatch


def _walk(op):
    return [(p[0], p[2]) for p in op._get_provider_chain("auto")]


def _drawn(view):
    return [(r["backend"], r["model"]) for r in view["rungs"] if not r["skip"]]


@pytest.mark.parametrize("selected", ["", "ollama", "groq", "anthropic", "openrouter", "gemini", "deepseek"])
def test_the_drawn_chain_is_the_walk(keys, selected):
    op = _operator(selected_backend=selected)
    view = op._default_chain_view()
    assert view["fallback"] is True
    assert _drawn(view) == _walk(op)
    # Everything registered is somewhere: on the chain, or listed as manual.
    drawn = {r["backend"] for r in view["rungs"]} | {r["backend"] for r in view["manual"]}
    assert drawn == {"ollama", "anthropic", *OPENAI_COMPAT_PROVIDERS}
    assert [r["backend"] for r in view["rungs"] if r["backend"] != selected] == \
        [b for b in LLM_FALLBACK_ORDER if b != selected]


@pytest.mark.parametrize("missing", ["GROQ_API_KEY", "ANTHROPIC_API_KEY"])
def test_a_rung_with_no_key_is_drawn_skipped_and_the_walk_leaves_it_out(keys, missing):
    keys.delenv(missing)
    op = _operator(selected_backend="ollama")
    view = op._default_chain_view()
    skipped = [r for r in view["rungs"] if r["skip"]]
    assert [r["skip"] for r in skipped] == ["no key"], view["rungs"]
    assert skipped[0]["backend"] not in [b for b, _ in _walk(op)]
    assert _drawn(view) == _walk(op)


def test_a_rung_with_a_key_and_no_model_is_drawn_skipped(keys):
    op = _operator(selected_backend="ollama")
    op.config["llm"]["fallback"] = [f for f in op.config["llm"]["fallback"] if f["provider"] != "xai"]
    view = op._default_chain_view()
    assert {r["backend"]: r["skip"] for r in view["rungs"]}["xai"] == "no model"
    assert _drawn(view) == _walk(op)


def test_a_selection_that_cannot_be_called_is_drawn_first_and_skipped(keys):
    """The walk drops it and starts at the next rung; the page must show the
    selection is still the selection, and that it is not what answers."""
    keys.delenv("ANTHROPIC_API_KEY")
    op = _operator(selected_backend="anthropic")
    view = op._default_chain_view()
    assert (view["rungs"][0]["backend"], view["rungs"][0]["skip"]) == ("anthropic", "no key")
    assert _drawn(view) == _walk(op)


def test_fallback_off_is_reported_the_way_the_walk_reads_it(keys):
    op = _operator(selected_backend="ollama", allow_paid_escalation="false")
    view = op._default_chain_view()
    assert view["fallback"] is False
    assert _walk(op) == _drawn(view)[:1], "with fallback off the walk is the first usable rung alone"
    # Unset is ON for the walk, so it is on in the picture too.
    assert _operator()._default_chain_view()["fallback"] is True


def test_the_view_survives_a_database_that_cannot_be_read(keys):
    op = _operator()
    op.kb.get_setting = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))
    view = op._default_chain_view()
    assert [r["backend"] for r in view["rungs"]] == list(LLM_FALLBACK_ORDER)
    assert view["rungs"][0]["model"] == "local-m"


# ---- the console routes ----------------------------------------------------

@pytest.fixture
def console(server):
    """The provider-registry fixture's operator is a MagicMock; bind the
    agent's real resolvers onto it so the routes read what the agent would."""
    op = server.operator
    op.config = {"llm": {"primary": {"url": "http://ollama:11434", "model": "local-m"},
                         "fallback": [{"provider": "groq", "model": "groq-m"}]},
                 "remediation": {}}
    for name in ("_default_chain_view", "_configured_model_for", "provider_api_key", "_scope_llm",
                 "_scope_llm_config", "_triage_model", "_judge_model", "_judge_model_setting",
                 "_judge_model_config", "_executor_config"):
        setattr(op, name, MethodType(getattr(CFOperator, name), op))
    return server


def test_the_chain_route_labels_every_rung(console, monkeypatch):
    c = console.app.test_client()
    data = c.get("/api/settings/llm_chain").get_json()
    assert [r["backend"] for r in data["rungs"]] == list(LLM_FALLBACK_ORDER)
    assert all(r["label"] for r in data["rungs"] + data["manual"])
    groq = next(r for r in data["rungs"] if r["backend"] == "groq")
    assert (groq["model"], groq["skip"]) == ("groq-m", "no key")
    monkeypatch.setenv("GROQ_API_KEY", "k")
    groq = next(r for r in c.get("/api/settings/llm_chain").get_json()["rungs"] if r["backend"] == "groq")
    assert groq["skip"] == ""


def test_picking_a_rungs_model_does_not_move_the_rung(console):
    c = console.app.test_client()
    console.settings["selected_backend"] = "ollama"
    resp = c.post("/api/models/groq/select", json={"model": "other-m", "default": False})
    assert resp.status_code == 200 and resp.get_json()["default"] is False
    assert console.settings["groq_selected_model"] == "other-m"
    assert console.settings["selected_backend"] == "ollama"
    rungs = c.get("/api/settings/llm_chain").get_json()["rungs"]
    assert [(r["backend"], r["model"]) for r in rungs][:2] == [("ollama", "local-m"), ("groq", "other-m")]
    # Without the flag it is the old contract: the backend becomes the selection.
    assert c.post("/api/models/groq/select", json={"model": "other-m"}).get_json()["default"] is True
    assert console.settings["selected_backend"] == "groq"


def test_investigate_is_listed_and_cannot_be_moved(console):
    c = console.app.test_client()
    rows = {r["scope"]: r for r in c.get("/api/settings/llm_scopes").get_json()["scopes"]}
    assert rows["investigate"]["editable"] is False and rows["investigate"]["kind"] == "default"
    assert c.put("/api/settings/llm_scopes/investigate", json={"backend": "groq"}).status_code == 404
    assert c.delete("/api/settings/llm_scopes/investigate").status_code == 404


# ---- the page ---------------------------------------------------------------

def _page():
    return ADMIN.read_text(encoding="utf-8")


def _flow(html):
    """The LLM_FLOW literal: the page's layout of steps."""
    m = re.search(r"const LLM_FLOW = \[(.*?)\n\];", html, flags=re.S)
    assert m, "admin.html has no LLM_FLOW layout"
    return m.group(1)


def test_every_step_the_server_lists_has_a_place_on_the_page(console):
    flow = _flow(_page())
    listed = [r["scope"] for r in console.app.test_client().get("/api/settings/llm_scopes").get_json()["scopes"]]
    assert set(LLM_SCOPES) <= set(listed)
    judges = [s for s in listed if s.startswith("judge:")]
    assert judges and "judges:true" in flow, "the judge pickers have no step"
    for scope in listed:
        if scope in judges:
            continue
        assert f"scope:'{scope}'" in flow, (
            f"{scope} is listed by /api/settings/llm_scopes and has no place in LLM_FLOW; "
            "decide where it sits in the flow (it renders under Other until then)")


def test_a_step_the_layout_does_not_know_is_still_shown():
    html = _page()
    assert "!placed.has(s.scope)" in html and "section('Other'" in html


def test_the_page_draws_the_chain_from_the_server_and_keeps_the_order_on_a_model_pick():
    html = _page()
    assert "/api/settings/llm_chain" in html
    pick = html[html.index("async function onRungModel"):]
    pick = pick[:pick.index("\n}")]
    assert "default:false" in pick, "a rung's model pick would move the rung to the front"
    # The order is the server's: no copy of it in the page.
    assert not re.search(r"\[\s*'ollama'\s*,\s*'groq'", html)


def test_the_chain_route_is_readable_like_the_other_settings_reads():
    """A GET, like /api/settings/llm_scopes beside it; the writes it leads to
    (/select, /provider, /fallback) carry the admin gate."""
    src = (REPO_ROOT / "web_server.py").read_text(encoding="utf-8")
    for handler in ("def select_model", "def set_selected_provider", "def set_fallback_settings"):
        preceding = src[:src.index(handler)].rsplit("@self.app.route", 1)[1]
        assert "require_role(ROLE_ADMIN)" in preceding, f"{handler} lost its ROLE_ADMIN gate"
