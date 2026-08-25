"""HTTP-level tests for the learnings write seam (CFOP-47).

Through the real WebServer._setup_routes() Flask app — the CFOP-49 lesson:
pure-policy tests leave the handler deletable, so the guard has to live at the
layer the defect would. Auth is installed in dev-bypass mode; who may call
these routes is covered by test_web_auth_db.py (require_token_scope /
require_role behavior against a real token store).
"""

import os
import threading
from unittest.mock import MagicMock

import pytest


def _client(**kb_overrides):
    """Flask test client wired to a stub operator (same shape as the console
    tests in test_remediation_queue.py)."""
    from web_server import WebServer
    from web_auth import install_auth
    from flask import Flask

    operator = MagicMock()
    operator.kb.store_learning.return_value = 123
    operator.kb.deprecate_learning.return_value = True
    for name, value in kb_overrides.items():
        setattr(operator.kb, name, value)

    server = WebServer.__new__(WebServer)
    server.operator = operator
    server.host, server.port = "localhost", 0
    server.app = Flask(__name__)
    server._chat_sessions = {}
    server._sessions_lock = threading.Lock()
    server._setup_routes()

    prior = os.environ.get("CFOP_AUTH_DISABLED")
    os.environ["CFOP_AUTH_DISABLED"] = "true"
    try:
        install_auth(server.app, ui_dir="ui")
    finally:
        if prior is None:
            os.environ.pop("CFOP_AUTH_DISABLED", None)
        else:
            os.environ["CFOP_AUTH_DISABLED"] = prior

    return server.app.test_client(), operator


def _body(**over):
    base = {"learning_type": "insight", "title": "pi fleet",
            "description": "three Pis run k3s", "applies_when": "alerts on pi hosts"}
    base.update(over)
    return base


# ---- POST /api/learnings -----------------------------------------------------


def test_post_learning_stores_and_returns_201():
    client, op = _client()
    resp = client.post("/api/learnings", json=_body(services=["k3s"], tags=["arm"]))
    assert resp.status_code == 201
    assert resp.get_json()["id"] == 123
    stored = op.kb.store_learning.call_args.args[0]
    assert stored["title"] == "pi fleet"
    assert stored["applies_when"] == "alerts on pi hosts"
    assert stored["services"] == ["k3s"]


def test_post_learning_folds_provenance_into_tags():
    """source/inferred/confidence have no columns; they must land as
    searchable tags, or discovery-seeded rows are indistinguishable from
    human-curated ones."""
    client, op = _client()
    resp = client.post("/api/learnings", json=_body(
        source="discovery", inferred=True, confidence=0.8, tags=["arm"]))
    assert resp.status_code == 201
    tags = op.kb.store_learning.call_args.args[0]["tags"]
    assert "arm" in tags
    assert "source:discovery" in tags
    assert "inferred" in tags
    assert "confidence:0.80" in tags


@pytest.mark.parametrize("missing", ["learning_type", "title", "description", "applies_when"])
def test_post_learning_requires_fields(missing):
    """applies_when especially: the KB auto-deprecates trigger-less learnings,
    so accepting one would return success while seeding nothing."""
    client, op = _client()
    resp = client.post("/api/learnings", json=_body(**{missing: ""}))
    assert resp.status_code == 400
    assert missing in resp.get_json()["error"]
    op.kb.store_learning.assert_not_called()


def test_post_learning_rejects_unknown_type():
    client, op = _client()
    assert client.post("/api/learnings", json=_body(learning_type="prophecy")).status_code == 400
    op.kb.store_learning.assert_not_called()


def test_post_learning_rejects_non_object_body():
    client, _ = _client()
    resp = client.post("/api/learnings", data="not json",
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


def test_post_learning_kb_offline_is_503():
    """The buffered KB returns -1 when the DB is down; 201 would silently
    vanish the learning."""
    client, _ = _client(store_learning=MagicMock(return_value=-1))
    assert client.post("/api/learnings", json=_body()).status_code == 503


def test_post_learning_ignores_bogus_confidence():
    client, op = _client()
    client.post("/api/learnings", json=_body(confidence="very"))
    assert not any(t.startswith("confidence:")
                   for t in op.kb.store_learning.call_args.args[0]["tags"])


def test_post_learning_caps_field_sizes():
    client, op = _client()
    client.post("/api/learnings", json=_body(title="x" * 1000))
    assert len(op.kb.store_learning.call_args.args[0]["title"]) == 500


# ---- DELETE /api/learnings/<id> ----------------------------------------------


def test_delete_learning_deprecates():
    client, op = _client()
    resp = client.delete("/api/learnings/7")
    assert resp.status_code == 200
    assert resp.get_json() == {"id": 7, "deprecated": True}
    op.kb.deprecate_learning.assert_called_once_with(7)


def test_delete_unknown_learning_is_404():
    client, _ = _client(deprecate_learning=MagicMock(return_value=False))
    assert client.delete("/api/learnings/999").status_code == 404
