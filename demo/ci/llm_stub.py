#!/usr/bin/env python3
"""Ollama-compatible scripted LLM for the demo CI e2e (CFOP-31).

The release-gate e2e asserts *pipeline mechanics* — a fault becomes a firing
alert, the alert is polled, triaged, investigated, recorded, and the second
investigation of the same fault class carries persisted similar-past
citations. None of that is a claim about model quality, and a real model on a
GitHub runner is both too slow and non-deterministic — the issue's own
acceptance is "deterministically". So CI points OLLAMA_URL at this.

Serves just enough of the Ollama API:

  GET  /api/tags        -> the configured model (embeddings availability probe)
  POST /api/chat        -> triage prompts get a fixed "investigate" JSON;
                           investigation prompts get a fixed STATUS: monitoring
                           final (no tool calls, so no kubectl dependency and
                           no remediation-queue path in CI)
  POST /api/embeddings  -> a constant 768-dim vector. Constant is deliberate:
                           the two CI investigations then have cosine
                           similarity 1.0, so the hybrid search MUST cite the
                           first from the second — which is the assertion.

Usage: python3 demo/ci/llm_stub.py [port]   (default 11434)
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "demo-stub"
DIM = 768  # nomic-embed-text dimension; must match the pgvector column

TRIAGE_RESPONSE = json.dumps({
    "action": "investigate",
    "reason": "demo fault, no precedent required",
    "confidence": 0.95,
})

INVESTIGATION_RESPONSE = (
    "Demo pipeline investigation: the alert was received from Alertmanager and "
    "processed end to end by the scripted CI model.\n"
    "STATUS: monitoring\n"
    "RECOMMENDATION: No action needed"
)


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: dict, code: int = 200) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            self._send({"models": [{"name": MODEL}]})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send({"error": "bad json"}, 400)
            return

        if self.path.startswith("/api/embeddings"):
            self._send({"embedding": [0.1] * DIM})
            return

        if self.path.startswith("/api/chat"):
            text = " ".join(
                str(m.get("content", "")) for m in payload.get("messages", [])
            )
            content = (
                TRIAGE_RESPONSE if "triage classifier" in text
                else INVESTIGATION_RESPONSE
            )
            self._send({
                "message": {"role": "assistant", "content": content},
                "done": True,
                "prompt_eval_count": 10,
                "eval_count": 10,
            })
            return

        self._send({"error": "not found"}, 404)

    def log_message(self, fmt, *args):  # noqa: A002 - quiet by default
        sys.stderr.write("llm-stub: " + fmt % args + "\n")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 11434
    print(f"llm-stub listening on :{port} (model {MODEL})", flush=True)
    # Threading: triage and embedding calls can overlap on a slow runner, and
    # a single-threaded server would serialize them into timeout territory.
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
