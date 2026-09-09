#!/usr/bin/env python3
"""Triage host routing — _triage_url and run_triage's use of it (CFOP-175).

Triage was hardwired to llm.primary.url, so the triage model could only run on
the same ollama host as investigations. _triage_url lets it name its own host
(local or remote) and fall back to primary when unset. These guard the class of
regression: a default deployment must reach primary unchanged, config and DB
overrides must win in that order, and run_triage must actually POST to the
resolved host — a future edit that reverts to primary.url fails the last test.
"""

import os
import sys
from queue import Queue
from unittest.mock import MagicMock
import threading as _t

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator


def _operator(config=None, db=None):
    op = CFOperator.__new__(CFOperator)
    op.config = config if config is not None else {}
    op.embeddings = MagicMock()
    op.embeddings.is_available.return_value = False
    op.kb = MagicMock()
    op.kb.get_setting.side_effect = lambda k, d='': (db or {}).get(k, d)
    op._investigation_lock = _t.Lock()
    op._investigation_queue = Queue(maxsize=8)
    return op


def _primary(url):
    return {"llm": {"primary": {"provider": "ollama", "url": url}}}


# --- _triage_url resolution ---------------------------------------------------

def test_triage_url_defaults_to_primary(monkeypatch):
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    op = _operator(_primary("http://ubuntu-llm-01:11434"))
    assert op._triage_url() == "http://ubuntu-llm-01:11434"


def test_config_triage_url_overrides_primary():
    cfg = _primary("http://ubuntu-llm-01:11434")
    cfg["llm"]["triage_url"] = "http://192.168.0.232:11434"
    assert _operator(cfg)._triage_url() == "http://192.168.0.232:11434"


def test_db_triage_url_beats_config():
    cfg = _primary("http://ubuntu-llm-01:11434")
    cfg["llm"]["triage_url"] = "http://from-config:11434"
    op = _operator(cfg, db={"triage_url": "http://from-db:11434"})
    assert op._triage_url() == "http://from-db:11434"


def test_blank_db_and_config_fall_through_to_primary(monkeypatch):
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    cfg = _primary("http://ubuntu-llm-01:11434")
    cfg["llm"]["triage_url"] = "   "  # whitespace is "unset"
    op = _operator(cfg, db={"triage_url": ""})
    assert op._triage_url() == "http://ubuntu-llm-01:11434"


def test_env_ollama_url_is_the_last_resort(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://env-host:11434")
    op = _operator({"llm": {"primary": {"provider": "ollama"}}})  # no url anywhere
    assert op._triage_url() == "http://env-host:11434"


def test_db_read_failure_falls_back_to_config(monkeypatch):
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    cfg = _primary("http://primary:11434")
    cfg["llm"]["triage_url"] = "http://config-host:11434"
    op = _operator(cfg)
    op.kb.get_setting.side_effect = RuntimeError("db down")
    assert op._triage_url() == "http://config-host:11434"


# --- run_triage actually routes there ----------------------------------------

def _alert():
    return {"severity": "warning", "summary": "Pod foo not ready",
            "labels": {}, "details": {}, "alert_id": "t-1"}


def _route_capture(op):
    """Stub the single-shot chat and record the url it was POSTed to."""
    seen = {}

    def fake_chat(*, provider_type, url, model, messages, system_context, max_iterations):
        seen["url"] = url
        seen["model"] = model
        return {"response": '{"action": "notify", "reason": "ok", "confidence": 0.8}',
                "tool_calls": 0}
    op._chat_with_tools = fake_chat
    return seen


def test_run_triage_posts_to_the_triage_host_not_primary():
    cfg = _primary("http://ubuntu-llm-01:11434")
    cfg["llm"]["triage_url"] = "http://192.168.0.232:11434"
    op = _operator(cfg, db={"triage_model": "cfop-triage-ministral3:v5-q4"})
    seen = _route_capture(op)
    result = op.run_triage(_alert())
    assert result["action"] == "notify"          # the triage-model branch served it
    assert seen["url"] == "http://192.168.0.232:11434"
    assert seen["model"] == "cfop-triage-ministral3:v5-q4"


def test_run_triage_uses_primary_when_no_triage_url(monkeypatch):
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    cfg = _primary("http://ubuntu-llm-01:11434")
    op = _operator(cfg, db={"triage_model": "cfop-triage-ministral3:v5-q4"})
    seen = _route_capture(op)
    op.run_triage(_alert())
    assert seen["url"] == "http://ubuntu-llm-01:11434"


def test_malformed_llm_config_degrades_to_chain_not_raise():
    """A broken llm/primary (here: primary is a string) makes _triage_url raise.
    That must degrade to the standard provider chain — the whole point of the
    dedicated-model branch — not throw out of run_triage. Guards the reviewer's
    finding that the resolve call must sit inside the try (CFOP-175 review)."""
    op = _operator({"llm": {"primary": "not-a-dict"}},
                   db={"triage_model": "cfop-triage-ministral3:v5-q4"})
    op._chat_with_tools = MagicMock(side_effect=AssertionError("must not reach the triage host"))
    op._chat_with_tools_with_fallback = MagicMock(return_value={
        "response": '{"action": "investigate", "reason": "chain served it", "confidence": 0.8}',
        "tool_calls": 0, "backend": "ollama", "model": "gemma4:26b"})
    result = op.run_triage(_alert())          # must not raise
    assert result["action"] == "investigate"
    op._chat_with_tools_with_fallback.assert_called_once()
