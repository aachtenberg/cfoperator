"""The console's manual enqueue is asked of the dedupe shadow too (CFOP-514).

POST /api/remediations decides on the exact key alone. The same question goes
to cfop-dedupe around the enqueue, so the console path's by-design
disagreements are counted under path=console, not missed. The agent-side
tests cover the shadow itself; this pins the route's wiring.
"""

from repo_paths import REPO_ROOT
import os
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

# agent.py imports its siblings bare, so the package directory goes on the path
# after the repo root, the same arrangement as test_console_provider_registry.
sys.path.insert(0, str(REPO_ROOT))
sys.path.append(os.path.join(str(REPO_ROOT), "agent"))


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("CFOP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CFOP_AUTH_DB_DISABLED", "true")
    op = MagicMock()
    op.config = {}
    from web_server import WebServer
    ws = WebServer(op)
    ws.app.config["TESTING"] = True
    return ws, op


def test_the_manual_enqueue_runs_inside_the_shadow_scope(server):
    ws, op = server
    log = []

    @contextmanager
    def scope(path, details):
        log.append(("enter", path, {k: v for k, v in details.items() if v is not None}))
        yield None
        log.append(("exit",))
    op._dedupe_shadow_scope = scope
    op.kb.queue_remediation.side_effect = lambda **kw: log.append(("queue",)) or 12
    op.kb.get_remediation.return_value = {"id": 12}

    resp = ws.app.test_client().post("/api/remediations", json={
        "recommendation": "Replace the SD card in raspberrypi3",
        "title": "rpi3 SD card", "host_id": "raspberrypi3", "dedupe_key": "manual-rpi3-sd"})
    assert resp.status_code == 201, resp.get_json()
    assert [e[0] for e in log] == ["enter", "queue", "exit"]
    assert log[0][1:] == ("console", {
        "dedupe_key": "manual-rpi3-sd", "recommendation": "Replace the SD card in raspberrypi3",
        "host": "raspberrypi3", "title": "rpi3 SD card", "remediation_class": "manual",
        "source": "manual"})
