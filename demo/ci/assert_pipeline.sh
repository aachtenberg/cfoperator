#!/usr/bin/env bash
# CFOP-31 CI assertions: poll the agent API until the pipeline produced what
# the e2e stage expects, or time out loudly.
#
#   demo/ci/assert_pipeline.sh first  600   # a demo-faults investigation completed
#   demo/ci/assert_pipeline.sh cited  600   # ...and a later one carries
#                                           # findings.similar_past citations
#
# Runs inside the event-runtime container (same pattern as scripts/
# demo-alert.sh): it already holds the API token bootstrap minted, so CI needs
# no credentials of its own.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

MODE="${1:?usage: assert_pipeline.sh first|cited <timeout-seconds>}"
TIMEOUT="${2:-600}"

COMPOSE=(docker compose -f docker-compose.yml -f demo/docker-compose.demo.yml)

CFOP_ASSERT_MODE="$MODE" CFOP_ASSERT_TIMEOUT="$TIMEOUT" \
"${COMPOSE[@]}" exec -T -e CFOP_ASSERT_MODE -e CFOP_ASSERT_TIMEOUT event-runtime python3 - <<'PY'
import json, os, sys, time, urllib.request

def token(path="/shared/event-runtime.env"):
    try:
        for line in open(path):
            if line.startswith("CFOP_API_TOKEN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return os.getenv("CFOP_API_TOKEN", "")

MODE = os.environ["CFOP_ASSERT_MODE"]
DEADLINE = time.time() + int(os.environ["CFOP_ASSERT_TIMEOUT"])
AGENT = os.getenv("CFOP_AGENT_URL", "http://agent:8083").rstrip("/")
TOK = token()

def get(path):
    req = urllib.request.Request(f"{AGENT}{path}")
    if TOK:
        req.add_header("Authorization", f"Bearer {TOK}")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)

def investigations():
    return get("/api/investigations?limit=50").get("investigations", [])

def demo_rows(rows):
    return [r for r in rows
            if "demo-faults" in (r.get("trigger") or "")
            and r.get("outcome") not in (None, "", "in_progress")]

while time.time() < DEADLINE:
    try:
        rows = demo_rows(investigations())
    except Exception as e:
        print(f"  ...api not ready ({e})", flush=True)
        rows = []
    if MODE == "first" and rows:
        r = rows[0]
        print(f"OK: investigation #{r['id']} completed ({r['outcome']}): {r['trigger'][:90]}")
        sys.exit(0)
    if MODE == "cited":
        # The list rows are summaries; findings live on the detail endpoint.
        for r in rows:
            try:
                detail = get(f"/api/investigations/{r['id']}")
            except Exception:
                continue
            past = (detail.get("findings") or {}).get("similar_past") or []
            if past:
                print(f"OK: investigation #{r['id']} cites {len(past)} past investigation(s): "
                      + ", ".join(f"#{p.get('id')} (sim {p.get('similarity')})" for p in past))
                sys.exit(0)
    print(f"  ...waiting ({len(rows)} demo investigation(s) so far, mode={MODE})", flush=True)
    time.sleep(15)

print(f"FAIL: mode={MODE} not satisfied within timeout", file=sys.stderr)
sys.exit(1)
PY
