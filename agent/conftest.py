"""Agent-suite fixtures.

cfop-dedupe decides every remediation enqueue (CFOP-538). No test here may
reach a real service, and a test that wires a service URL needs an answer, so
every test gets the service's model (dedupe_reference.py, held to the shared
cases by test_dedupe_parity.py) in place of the HTTP call. A test that is about
the client itself takes ``dedupe_client.decide`` by reference at import time.
"""

import pytest

import dedupe_client
import dedupe_reference


@pytest.fixture(autouse=True)
def _dedupe_service_model(monkeypatch):
    dedupe_reference.REQUESTS.clear()
    monkeypatch.setattr(dedupe_client, "decide", dedupe_reference.serve)
    yield
    dedupe_reference.REQUESTS.clear()
