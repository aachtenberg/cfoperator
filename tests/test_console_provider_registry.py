"""The Admin LLM switcher can select every provider the agent can speak (CFOP-104).

Gemini was registered on the agent (``OPENAI_COMPAT_PROVIDERS``), named in
Helm, compose and ``.env.example``, accepted by ``_resolve_provider`` — and
unreachable from the console, because ``/api/providers``, ``/api/models``
and the ``POST /api/settings/provider`` allowlist were each a hand-copied
list that stopped at xai. These tests guard the class: a provider in the
agent's registry is listed, listable and selectable through the switcher,
with the model list fetched from the registry's own base URL.

No live provider is called: ``requests.get`` is replaced for the models test.
"""

from repo_paths import REPO_ROOT
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = REPO_ROOT
# agent/agent.py imports its siblings bare (``knowledge_base``), so the package
# directory has to be on the path as well as the repo root — root FIRST, or
# ``agent`` resolves to agent/agent.py instead of the package. Same arrangement
# as test_docs_fix_contract.py and the agent suite in tests.yml.
sys.path.insert(0, str(ROOT))
sys.path.append(os.path.join(str(ROOT), "agent"))

from agent import OPENAI_COMPAT_PROVIDERS  # noqa: E402


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("CFOP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CFOP_AUTH_DB_DISABLED", "true")
    for cfg in OPENAI_COMPAT_PROVIDERS.values():
        monkeypatch.delenv(cfg["key_env"], raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    settings = {}
    op = MagicMock()
    op.config = {"llm": {"primary": {"url": "http://ollama:11434"}}}
    op.kb.get_setting.side_effect = lambda key, default="": settings.get(key, default)
    op.kb.set_setting.side_effect = settings.__setitem__

    from web_server import WebServer

    ws = WebServer(op)
    ws.app.config["TESTING"] = True
    ws.settings = settings
    return ws


def _providers(client):
    resp = client.get("/api/providers")
    assert resp.status_code == 200
    return {p["id"]: p for p in resp.get_json()["providers"]}


def test_gemini_row_is_gated_on_its_key(server, monkeypatch):
    c = server.app.test_client()
    row = _providers(c)["gemini"]
    assert row["available"] is False
    assert row["name"]

    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    assert _providers(c)["gemini"]["available"] is True


def test_every_registered_compat_provider_is_listed_and_selectable(server, monkeypatch):
    c = server.app.test_client()
    for backend, cfg in OPENAI_COMPAT_PROVIDERS.items():
        listed = _providers(c)
        assert backend in listed, f"{backend} is registered on the agent but not offered"
        assert listed[backend]["name"] == cfg["label"]
        # Availability follows the registry's own env var, not a copied name.
        assert listed[backend]["available"] is False
        monkeypatch.setenv(cfg["key_env"], "k")
        assert _providers(c)[backend]["available"] is True
        monkeypatch.delenv(cfg["key_env"])

        resp = c.post("/api/settings/provider", json={"backend": backend})
        assert resp.status_code == 200, (backend, resp.get_json())
        assert server.settings["selected_backend"] == backend


def test_provider_allowlist_still_rejects_unknown_backends(server):
    c = server.app.test_client()
    resp = c.post("/api/settings/provider", json={"backend": "bedrock"})
    assert resp.status_code == 400
    assert "selected_backend" not in server.settings


def test_every_registered_compat_provider_lists_models_from_its_base_url(server, monkeypatch):
    import web_server

    for backend, cfg in OPENAI_COMPAT_PROVIDERS.items():
        monkeypatch.setenv(cfg["key_env"], f"{backend}-key")
        server.settings[f"{backend}_selected_model"] = "chosen"
        calls = []

        def fake_get(url, headers=None, timeout=None):
            calls.append((url, headers))
            resp = MagicMock()
            resp.json.return_value = {"data": [{"id": "b-model"}, {"id": "a-model"}]}
            return resp

        monkeypatch.setattr(web_server.requests, "get", fake_get)

        resp = server.app.test_client().get(f"/api/models/{backend}")
        assert resp.status_code == 200, (backend, resp.get_json())
        assert resp.get_json() == {"models": ["a-model", "b-model"], "selected": "chosen"}
        (url, headers), = calls
        assert url == cfg["base_url"].rstrip("/") + "/models", backend
        assert headers["Authorization"] == f"Bearer {backend}-key", backend

    # Google's OpenAI-compat surface has no /v1 segment; the registry's
    # base_url is what the agent chats through, and the listing follows it.
    assert OPENAI_COMPAT_PROVIDERS["gemini"]["base_url"] + "/models" == \
        "https://generativelanguage.googleapis.com/v1beta/openai/models"


def test_agent_resolves_every_registered_compat_provider(server):
    """The switcher persists selected_backend; the agent has to honour it.

    _resolve_provider used to gate UI-selected backends on its own copied
    tuple, so a registry key the console now accepts could be saved and then
    silently ignored as a preference."""
    from agent import CFOperator

    for backend in OPENAI_COMPAT_PROVIDERS:
        op = MagicMock()
        op.config = {"llm": {}}
        # Explicit backend, explicit model: nothing to look up.
        assert CFOperator._resolve_provider(op, backend, "m") == (backend, None, "m")
        # 'auto' with the console's saved preference.
        op.kb.get_setting.side_effect = lambda key, default="": (
            backend if key == "selected_backend" else default)
        assert CFOperator._resolve_provider(op, "auto", "m") == (backend, None, "m")


def test_models_without_a_key_names_the_missing_variable(server):
    resp = server.app.test_client().get("/api/models/gemini")
    assert resp.status_code == 500
    assert "GEMINI_API_KEY" in resp.get_json()["error"]


def test_ask_sre_backend_docs_name_every_registered_provider():
    """ask_sre(backend=...) passes straight through to the agent, so its
    documented union must not stop short of what the switcher accepts."""
    for rel in ("mcp_server/tools/chat.py", "mcp_server/client.py",
                "docs/mcp-server.md", "docs/slack-bridge.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for backend in OPENAI_COMPAT_PROVIDERS:
            assert backend in text, f"{rel} does not name backend {backend!r}"


def test_namespaced_listing_ids_are_stripped_on_list_select_and_read(server, monkeypatch):
    """CFOP-112: Google lists ``models/gemini-…``; the switcher persisted it verbatim.

    Guards the class through the registry: any provider that declares a
    ``model_id_prefix`` is listed bare, persists bare, and a selection stored
    before the strip still reads back bare — so the chip, the judge floor and
    the docs name the same id, and the live selection keeps working.
    """
    import web_server

    prefixed = [(b, cfg) for b, cfg in OPENAI_COMPAT_PROVIDERS.items() if cfg.get("model_id_prefix")]
    assert prefixed, "the guard needs a namespaced provider in the registry (gemini today)"
    for backend, cfg in prefixed:
        prefix = cfg["model_id_prefix"]
        monkeypatch.setenv(cfg["key_env"], "k")
        # Stored before this change, with the prefix on.
        server.settings[f"{backend}_selected_model"] = f"{prefix}stored-model"

        def fake_get(url, headers=None, timeout=None):
            resp = MagicMock()
            resp.json.return_value = {"data": [{"id": f"{prefix}b-model"}, {"id": "a-model"}]}
            return resp

        monkeypatch.setattr(web_server.requests, "get", fake_get)
        c = server.app.test_client()

        resp = c.get(f"/api/models/{backend}")
        assert resp.status_code == 200, (backend, resp.get_json())
        assert resp.get_json() == {"models": ["a-model", "b-model"], "selected": "stored-model"}

        resp = c.post(f"/api/models/{backend}/select", json={"model": f"{prefix}picked"})
        assert resp.status_code == 200, (backend, resp.get_json())
        assert server.settings[f"{backend}_selected_model"] == "picked"

        assert c.get("/api/settings/provider").get_json() == {"backend": backend, "model": "picked"}
