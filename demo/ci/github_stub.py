#!/usr/bin/env python3
"""In-memory GitHub REST stub for the demo CI e2e's PR leg (CFOP-315).

The executor's whole output is a pull request, so the release e2e needs
somewhere for that PR to land that is not GitHub: no token secret in a release
job, no network flake, no scratch repo filling up with PRs. This serves the
endpoints executor/github.py calls, and nothing else. test_demo_kind.py fails
if the executor starts calling one this does not serve.

It holds one repository, REPO, seeded with demo/manifests/pending.yaml at the
root: the manifest the taint fault applies, which is the file the executor is
supposed to patch.

  GET  /repos/R/git/ref/heads/B        branch head, 404 if the branch is unknown
  GET  /repos/R/git/trees/SHA          the file list (recursive)
  GET  /repos/R/contents/PATH?ref=B    one file, base64
  POST /repos/R/git/refs               create a branch
  PUT  /repos/R/contents/PATH          commit a file to a branch
  POST /repos/R/pulls                  open a PR

  GET  /_stub/pulls                    what was opened, with each PR's files,
                                       for the e2e assertion

Usage: python3 demo/ci/github_stub.py [port]   (default 9300)
"""

import base64
import hashlib
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

REPO = "cfop-demo/scratch"
BASE = "main"
SEED_PATH = "pending.yaml"
SEED_FILE = Path(__file__).resolve().parent.parent / "manifests" / "pending.yaml"


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class Repo:
    """Branches map to {path: content}; a branch's sha is a hash of its files."""

    def __init__(self, files):
        self.lock = threading.Lock()
        self.branches = {BASE: dict(files)}
        self.pulls = []

    def head(self, branch):
        files = self.branches.get(branch)
        if files is None:
            return None
        return _sha(json.dumps(sorted(files.items())))

    def files_at(self, sha):
        for files in self.branches.values():
            if _sha(json.dumps(sorted(files.items()))) == sha:
                return files
        return None


STATE = Repo({SEED_PATH: SEED_FILE.read_text(encoding="utf-8")})

_ROUTE = re.compile(r"^/repos/(?P<repo>[^/]+/[^/]+)/(?P<rest>.+)$")


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, code=200):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _no_route(self):
        # Distinct from GitHub's own "Not Found" (unknown branch, file, sha), so
        # test_demo_kind.py can tell a route this stub lacks from a resource
        # that does not exist.
        self._send({"message": f"stub: no route for {self.command} {self.path}"}, 404)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return None

    def _route(self):
        url = urlparse(self.path)
        m = _ROUTE.match(url.path)
        if not m or m.group("repo") != REPO:
            return None, None, url
        return m.group("repo"), m.group("rest"), url

    def do_GET(self):
        if self.path.startswith("/_stub/pulls"):
            with STATE.lock:
                self._send(STATE.pulls)
            return
        repo, rest, url = self._route()
        if repo is None:
            return self._send({"message": "Not Found"}, 404)
        with STATE.lock:
            if rest.startswith("git/ref/heads/"):
                sha = STATE.head(rest[len("git/ref/heads/"):])
                if sha is None:
                    return self._send({"message": "Not Found"}, 404)
                return self._send({"object": {"sha": sha, "type": "commit"}})
            if rest.startswith("git/trees/"):
                files = STATE.files_at(rest[len("git/trees/"):])
                if files is None:
                    return self._send({"message": "Not Found"}, 404)
                return self._send({"tree": [{"path": p, "type": "blob"} for p in sorted(files)]})
            if rest.startswith("contents/"):
                path = rest[len("contents/"):]
                ref = dict(p.split("=", 1) for p in url.query.split("&") if "=" in p).get("ref", BASE)
                content = (STATE.branches.get(ref) or {}).get(path)
                if content is None:
                    return self._send({"message": "Not Found"}, 404)
                return self._send({"encoding": "base64", "sha": _sha(content),
                                   "content": base64.b64encode(content.encode()).decode()})
        self._no_route()

    def do_POST(self):
        repo, rest, _ = self._route()
        body = self._body()
        if repo is None or body is None:
            return self._send({"message": "Not Found"}, 404)
        with STATE.lock:
            if rest == "git/refs":
                branch = str(body.get("ref", "")).removeprefix("refs/heads/")
                source = STATE.files_at(str(body.get("sha", "")))
                if not branch or source is None or branch in STATE.branches:
                    return self._send({"message": "Reference already exists or bad sha"}, 422)
                STATE.branches[branch] = dict(source)
                return self._send({"ref": f"refs/heads/{branch}"}, 201)
            if rest == "pulls":
                head, base = body.get("head"), body.get("base")
                if head not in STATE.branches or base not in STATE.branches:
                    return self._send({"message": "Validation Failed"}, 422)
                number = len(STATE.pulls) + 1
                changed = {p: c for p, c in STATE.branches[head].items()
                           if STATE.branches[base].get(p) != c}
                STATE.pulls.append({
                    "number": number, "title": body.get("title"), "head": head, "base": base,
                    "html_url": f"https://github.com/{REPO}/pull/{number}",
                    "files": changed,
                })
                return self._send({"number": number,
                                   "html_url": f"https://github.com/{REPO}/pull/{number}"}, 201)
        self._no_route()

    def do_PUT(self):
        repo, rest, _ = self._route()
        body = self._body()
        if repo is None or body is None or not rest.startswith("contents/"):
            return self._no_route()
        path = rest[len("contents/"):]
        with STATE.lock:
            files = STATE.branches.get(body.get("branch", ""))
            if files is None:
                return self._send({"message": "Branch not found"}, 404)
            if path in files and body.get("sha") != _sha(files[path]):
                return self._send({"message": "sha does not match"}, 409)
            files[path] = base64.b64decode(body.get("content", "")).decode("utf-8")
            return self._send({"content": {"path": path}}, 200)

    def log_message(self, fmt, *args):  # noqa: A002 - quiet by default
        sys.stderr.write("github-stub: " + fmt % args + "\n")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9300
    print(f"github-stub listening on :{port} (repo {REPO})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
