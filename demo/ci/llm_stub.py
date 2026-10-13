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
  POST /api/chat        -> triage prompts get a fixed "investigate" JSON.
                           Investigations of the unschedulable fault get
                           STATUS: needs_action with a structured gitops FIX
                           on the GitHub stub's repo, which is what feeds the
                           remediation queue (CFOP-315). Every other
                           investigation gets a fixed STATUS: monitoring, so
                           the crashloop memory beat never queues anything.
                           No tool calls either way: no kubectl dependency.
  POST /v1/chat/completions
                        -> the executor's two passes (OpenAI-compatible):
                           pick pending.yaml, then a unified diff adding the
                           toleration, computed against the same seed file
                           the GitHub stub serves.
  POST /api/embeddings  -> a constant 768-dim vector. Constant is deliberate:
                           the two CI investigations then have cosine
                           similarity 1.0, so the hybrid search MUST cite the
                           first from the second — which is the assertion.

Usage: python3 demo/ci/llm_stub.py [port]   (default 11434)
"""

import difflib
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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


# The PR leg (CFOP-315). REPO and the seed file must match github_stub.py;
# test_demo_kind.py holds the two together.
REPO = "cfop-demo/scratch"
SEED_PATH = "pending.yaml"
SEED_FILE = Path(__file__).resolve().parent.parent / "manifests" / "pending.yaml"
TAINT = "cfop-demo"  # demo/fault.sh TAINT_KEY

RECOMMENDATION = (f"Add a toleration for the {TAINT}=fault:NoSchedule taint to "
                  f"{SEED_PATH} in {REPO}.")

PENDING_FIX = {
    "targets": [{"kind": "gitops-manifest", "id": SEED_PATH, "repo": REPO}],
    "observed": [{"source": "kubectl -n demo-faults describe pod",
                  "value": f"0/1 nodes are available: 1 node(s) had untolerated taint "
                           f"{{{TAINT}: fault}}"}],
    "steps": [RECOMMENDATION],
    "verify": {"command": "kubectl -n demo-faults get pods -l fault=pending",
               "expect": "Running"},
    "rejected": [{"alternative": "remove the taint from the nodes",
                  "why_not": "the taint is deliberate; the workload should tolerate it"}],
    "risk": "low",
}

PENDING_RESPONSE = (
    "The pod is Pending because every node carries an untolerated taint "
    f"({TAINT}=fault:NoSchedule) and the pod declares no tolerations.\n"
    "STATUS: needs_action\n"
    f"RECOMMENDATION: {RECOMMENDATION}\n"
    f"FIX: {json.dumps(PENDING_FIX)}"
)


def toleration_diff() -> str:
    """The executor's pass-2 answer: a diff against the seeded manifest."""
    before = SEED_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
    after = []
    for line in before:
        after.append(line)
        if line.rstrip() == "  restartPolicy: Always":
            after += ["  tolerations:\n",
                      f"    - key: {TAINT}\n",
                      "      operator: Equal\n",
                      "      value: fault\n",
                      "      effect: NoSchedule\n"]
    assert after != before, "seed manifest lost its restartPolicy line"
    return "".join(difflib.unified_diff(before, after, f"a/{SEED_PATH}", f"b/{SEED_PATH}"))


def executor_reply(prompt: str) -> str:
    if "Candidate files:" in prompt:
        return SEED_PATH
    return f"Adds the toleration.\n\n```diff\n{toleration_diff()}```\n"


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
            if "triage classifier" in text:
                content = TRIAGE_RESPONSE
            elif "unschedulable" in text.lower():
                content = PENDING_RESPONSE
            else:
                content = INVESTIGATION_RESPONSE
            self._send({
                "message": {"role": "assistant", "content": content},
                "done": True,
                "prompt_eval_count": 10,
                "eval_count": 10,
            })
            return

        if self.path.rstrip("/").endswith("/chat/completions"):
            prompt = " ".join(str(m.get("content", ""))
                              for m in payload.get("messages", []))
            self._send({
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": executor_reply(prompt)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
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
