#!/usr/bin/env python3
"""CFOP-315: the PR leg of the demo e2e, asserted from the runner.

After `demo/fault.sh taint`, wait for the remediation row the taint
investigation queued, approve it as the admin, and wait until it carries a PR
URL. Then confirm that URL is one github_stub.py actually issued, and that the
file in that PR carries the toleration.

The row is approved, not left to auto-execute. An auto-eligible row first
goes to the mutation judge (CFOP-70), whose peers are frontier vendors CI
has no keys for, so it parks at needs-human, as it would on any install
without one. Approving is the operator's own gate. A faked judge verdict
would test the stub, not the gate.

Both checks are needed. A pr_url on the row with no matching PR in the stub
would mean the completion callback reported something that never happened.

  python3 demo/ci/assert_pr.py <timeout-seconds>

Env: CFOP_ADMIN_PASSWORD (the compose bootstrap seeds the admin from it),
CFOP_CONSOLE_PORT (default 8083), CFOP_E2E_GITHUB_STUB (default
http://127.0.0.1:9300).
"""

import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request

REPO = "cfop-demo/scratch"
AGENT = f"http://127.0.0.1:{os.getenv('CFOP_CONSOLE_PORT', '8083')}"
STUB = os.getenv("CFOP_E2E_GITHUB_STUB", "http://127.0.0.1:9300").rstrip("/")
# A parked row waits for a person; 'queued' and later mean the drain has it.
AWAITING_APPROVAL = {"needs-human"}

opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def call(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=20) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def login():
    password = os.environ.get("CFOP_ADMIN_PASSWORD", "")
    if not password:
        sys.exit("FAIL: CFOP_ADMIN_PASSWORD is unset; the bootstrap admin cannot log in")
    call("POST", f"{AGENT}/login", {"username": os.getenv("CFOP_ADMIN_USERNAME", "admin"),
                                     "password": password})


# Rows from before this run (a reused local stack) are not this fault's.
# Baseline on ids, not timestamps: the runner's clock and the database's
# need not agree. The taint row cannot exist yet when this starts, because
# DemoPodUnschedulable takes a minute or two to fire after fault.sh.
BASELINE = None


def all_rows():
    global BASELINE
    rows = call("GET", f"{AGENT}/api/remediations?limit=100").get("remediations", [])
    if BASELINE is None:
        BASELINE = max((int(r["id"]) for r in rows), default=0)
        print(f"  ignoring remediation rows up to #{BASELINE} (they predate this run)", flush=True)
    return [r for r in rows if int(r["id"]) > BASELINE]


def scratch_rows(rows):
    return [r for r in rows if (r.get("payload") or {}).get("repo") == REPO]


def degraded_taint_row(rows):
    """A row for the taint fault that lost its repo and class on the way.

    That is a FIX the agent refused, so the recommendation fell to the
    classifier and parked as manual. Approving it would 409, and waiting
    would only time out, so say what happened instead.
    """
    for r in rows:
        rec = str((r.get("payload") or {}).get("recommendation") or "")
        if "pending.yaml" in rec and r.get("remediation_class") == "manual":
            return r
    return None


def main():
    deadline = time.time() + int(sys.argv[1] if len(sys.argv) > 1 else 900)
    approved = set()
    logged_in = False
    while time.time() < deadline:
        try:
            if not logged_in:
                login()
                logged_in = True
            every = all_rows()
            rows = scratch_rows(every)
            lost = degraded_taint_row(every)
            if lost:
                sys.exit(f"FAIL: the taint recommendation became manual row #{lost['id']} "
                         f"(repo {(lost.get('payload') or {}).get('repo')!r}): the agent refused "
                         "its FIX. Look for 'FIX rejected' in the agent log.")
        except urllib.error.HTTPError as e:
            # A 4xx is an answer, not an agent still booting: fail with it
            # rather than retrying until the timeout hides it.
            if 400 <= e.code < 500:
                sys.exit(f"FAIL: {e.code} from {e.url}: {e.read()[:500]!r}")
            print(f"  ...agent not ready ({e})", flush=True)
            logged_in = False
            time.sleep(15)
            continue
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"  ...agent not ready ({e})", flush=True)
            logged_in = False
            time.sleep(15)
            continue

        for r in rows:
            if r.get("pr_url"):
                return check_stub(r)
            if r.get("status") in AWAITING_APPROVAL and r["id"] not in approved:
                print(f"  approving row #{r['id']} ({r.get('status')}, "
                      f"{r.get('remediation_class')}, last_error={r.get('last_error')!r})", flush=True)
                try:
                    call("POST", f"{AGENT}/api/remediations/{r['id']}/approve", {})
                except urllib.error.HTTPError as e:
                    sys.exit(f"FAIL: approve #{r['id']} answered {e.code}: {e.read()[:500]!r}")
                approved.add(r["id"])
        summary = ", ".join(f"#{r['id']}={r.get('status')}" for r in rows) or "none yet"
        print(f"  ...waiting for a PR (rows for {REPO}: {summary})", flush=True)
        time.sleep(15)
    sys.exit(f"FAIL: no remediation row for {REPO} carried a PR URL within the timeout")


def check_stub(row):
    pulls = json.load(urllib.request.urlopen(f"{STUB}/_stub/pulls", timeout=10))
    match = [p for p in pulls if p.get("html_url") == row["pr_url"]]
    if not match:
        sys.exit(f"FAIL: row #{row['id']} says {row['pr_url']}, but the GitHub stub "
                 f"issued {[p.get('html_url') for p in pulls]}")
    patched = match[0].get("files", {}).get("pending.yaml", "")
    if "tolerations:" not in patched or "key: cfop-demo" not in patched:
        sys.exit(f"FAIL: PR {row['pr_url']} does not add the cfop-demo toleration:\n{patched}")
    print(f"OK: remediation #{row['id']} ({row.get('status')}) -> {row['pr_url']}, "
          f"which adds the toleration to pending.yaml")


if __name__ == "__main__":
    main()
