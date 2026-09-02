"""Guards for the console LLM chip / quick switcher (CFOP-19).

CFOP-15 moved full LLM/OODA/pool controls into Admin. The console keeps a
compact readout + admin popover that posts the same settings APIs. These
tests pin the wiring so a future edit cannot silently drop the chip, send
a per-chat backend again, or hollow out the Admin LLM tab.
"""

from repo_paths import REPO_ROOT
from pathlib import Path

UI = REPO_ROOT / "ui"
INDEX = UI / "index.html"
ADMIN = UI / "admin.html"


def test_console_mounts_llm_switcher():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="llm-switcher"' in html
    assert 'id="llm-chip"' in html
    assert 'id="llm-popover"' in html
    assert 'id="llm-backend"' in html
    assert 'id="llm-model"' in html
    assert 'id="llm-model-custom"' in html
    assert "/admin?tab=llm" in html


def test_console_chat_still_uses_server_selected_backend():
    """Chat must not reintroduce a per-request backend from the chip."""
    html = INDEX.read_text(encoding="utf-8")
    start = html.index("function sendMessage(")
    end = html.index("function stopChat()", start)
    body = html[start:end]
    assert "backend: 'auto'" in body
    assert "llm-backend" not in body
    assert "getElementById('llm-model')" not in body


def test_admin_still_owns_full_llm_tab():
    html = ADMIN.read_text(encoding="utf-8")
    assert 'data-tab="llm"' in html
    assert 'id="panel-llm"' in html
    assert "/api/providers" in html
    assert "/api/settings/provider" in html
    assert "fallback-toggle" in html
    assert "max-iterations" in html
    assert "pool-toggles" in html


def test_console_surfaces_model_list_errors_and_free_text_fallback():
    html = INDEX.read_text(encoding="utf-8")
    assert "llm-model-err" in html
    assert "setModelUi" in html
    assert "No models returned — enter a model id" in html
    assert "llm-model-custom" in html
    # Save failures must not leave the popover silently desynced from the server.
    assert "failed to save provider" in html
    assert "failed to save model" in html
    # Dialog focus moves into the popover and back to the chip.
    assert "focusLlmPopover" in html
    assert "chip.focus()" in html


def test_admin_wires_triage_model_setting():
    """CFOP-58: the dedicated triage model is switchable live from Admin.

    Guards the wiring class: the select exists, loads and posts the settings
    endpoint, and the endpoint's POST stays admin-gated in web_server.py —
    a dropped decorator would let members repoint production triage.
    """
    html = ADMIN.read_text(encoding="utf-8")
    assert 'id="triage-model"' in html
    assert "/api/settings/triage_model" in html
    assert "onTriageModelChange" in html
    # 'off' must be offerable — disabling despite config is part of the contract.
    assert 'value="off"' in html

    src = (REPO_ROOT / "web_server.py").read_text(encoding="utf-8")
    idx = src.index("def set_triage_model")
    preceding = src[:idx].rsplit("@self.app.route", 1)[1]
    assert "require_role(ROLE_ADMIN)" in preceding, (
        "POST /api/settings/triage_model lost its ROLE_ADMIN gate"
    )


def test_console_renders_provider_fallback_events():
    """CFOP-112: a provider rotation inside a chat is shown, not swallowed.

    The server already emitted ``fallback`` when a rung failed mid-loop, and
    now also when the chip's selection is skipped (no key, no model); the
    console ignored the event, so the reply arrived labelled gemma4 under a
    chip that said Gemini with a bare spinner in between.
    """
    html = INDEX.read_text(encoding="utf-8")
    start = html.index("function handleChatEvent(evt)")
    end = html.index("function escapeHtml(text)", start)
    body = html[start:end]
    assert "type === 'fallback'" in body
    assert "addFallbackNote(data.from, data.to, data.reason)" in body
    assert "function addFallbackNote(from, to, reason)" in html
    assert ".tool-fallback" in html
