#!/usr/bin/env bash
# First-run demo: put one real alert through CFOperator and hand you the result.
#
#   ./scripts/demo-alert.sh
#
# Runs against the trial compose (docker-compose.yml). Needs nothing configured
# beyond what `docker compose up` already required.
#
# Two design decisions worth knowing, because both look like detours:
#
# 1. THE ALERT IS DISCOVERED, NOT FABRICATED. An earlier version of this demo
#    fired a made-up "pod demo/checkout-api is in CrashLoopBackOff". The agent
#    investigated it properly and correctly escalated, because a trial has no
#    Kubernetes and the pod does not exist. That is right behaviour and a
#    useless demo. So this script asks your Prometheus what is actually true
#    and builds the alert from that: a genuinely down target if there is one,
#    otherwise a verification request about a target that is up.
#
# 2. IT TALKS TO event_runtime THROUGH `docker compose exec`, not a published
#    port. POST /alert is an unauthenticated alert-injection endpoint in the
#    trial (CFOP_RUNTIME_TOKEN is unset), so the compose deliberately does not
#    put it on your LAN. Production does not need it either — event_runtime
#    polls Alertmanager rather than being pushed to.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

COMPOSE=(docker compose)
if [[ -n "${CFOP_COMPOSE_ENV_FILE:-}" ]]; then
  COMPOSE+=(--env-file "$CFOP_COMPOSE_ENV_FILE")
fi
if [[ -n "${CFOP_COMPOSE_PROJECT:-}" ]]; then
  COMPOSE+=(-p "$CFOP_COMPOSE_PROJECT")
fi

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
die() { printf '\n\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# ── 0. the stack has to be up ────────────────────────────────────────────────
"${COMPOSE[@]}" ps --status running --format '{{.Service}}' 2>/dev/null | grep -q '^event-runtime$' \
  || die "the trial stack is not running. Start it with:  docker compose up -d"

say "CFOperator first-run demo"

# Everything below runs inside the event-runtime container: it shares the
# agent's view of Prometheus, and it already holds an API token that bootstrap
# minted for it (read + investigate) — so the demo needs no credentials of its
# own and mints nothing new.
"${COMPOSE[@]}" exec -T event-runtime python3 - <<'PY'
import json, os, sys, time, urllib.error, urllib.parse, urllib.request

def env_file(path="/shared/event-runtime.env"):
    """The token bootstrap minted for this service. Same file the runtime sources."""
    try:
        for line in open(path):
            if line.startswith("CFOP_API_TOKEN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return os.getenv("CFOP_API_TOKEN", "")

TOKEN = env_file()
AGENT = os.getenv("CFOP_AGENT_URL", "http://agent:8083").rstrip("/")
PROM = os.getenv("PROMETHEUS_URL", "").rstrip("/")

def get(url, token=None, timeout=20):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)

def promql(expr):
    if not PROM:
        return []
    url = f"{PROM}/api/v1/query?query={urllib.parse.quote(expr)}"
    try:
        return get(url).get("data", {}).get("result", [])
    except Exception:
        return []

# ── 1. ask Prometheus what is actually true ──────────────────────────────────
print("  probing Prometheus for something real to investigate...")
if not PROM:
    print("  ! PROMETHEUS_URL is not set; the demo needs a metrics backend.")
    sys.exit(1)

down = promql("up == 0")
healthy = promql("up == 1")

# Each run carries a short nonce. Without it the second run of this demo is
# answered with `suppressed` rather than an investigation — recurrence
# suppression (identical finding inside the dedupe window) doing its job, which
# is correct behaviour and a broken demo. The nonce makes each run a genuinely
# distinct request instead of asking the noise filter to forget its purpose.
run_id = time.strftime("%H%M%S")

if down:
    labels = down[0].get("metric", {})
    job = labels.get("job", "unknown")
    inst = labels.get("instance", "unknown")
    summary = (f"Prometheus target '{job}' ({inst}) is down: up == 0. "
               f"Determine why it stopped reporting and whether anything depends on it. "
               f"[demo {run_id}]")
    severity, kind = "critical", "a real fault your Prometheus is reporting right now"
elif healthy:
    labels = healthy[0].get("metric", {})
    job = labels.get("job", "unknown")
    inst = labels.get("instance", "unknown")
    summary = (f"Verify Prometheus target '{job}' ({inst}) is healthy: confirm up == 1, "
               f"report how long it has been stable, and flag anything anomalous. "
               f"[demo {run_id}]")
    severity, kind = "warning", "a verification task (nothing is currently down)"
else:
    print("  ! Prometheus returned no 'up' series — is it scraping anything?")
    sys.exit(1)

print(f"  -> {kind}")
print(f"  -> {summary[:96]}{'...' if len(summary) > 96 else ''}")

# ── 2. hand it to the runtime exactly as an alert source would ───────────────
payload = {
    "source": "demo",
    "severity": severity,
    "summary": summary,
    "resource_type": "prometheus-target",
    "resource_name": inst,
    "details": {"job": job, "instance": inst, "demo": True},
}
# Remember the newest investigation id before submitting. Investigation rows
# carry no alert_id, so the only link back to this alert is its trigger text —
# and matching on that alone would still pick up an identical earlier run. The
# baseline makes "newer than what existed a moment ago" part of the match.
baseline = 0
try:
    prior = get(f"{AGENT}/api/investigations?limit=1", TOKEN)
    prior = prior if isinstance(prior, list) else prior.get("investigations", [])
    baseline = int(prior[0].get("id") or 0) if prior else 0
except Exception:
    pass  # a fresh stack has none; baseline 0 is correct

print("\n  submitting to event_runtime for triage...")
req = urllib.request.Request(
    "http://localhost:8080/alert?mode=sync",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.load(resp)
except urllib.error.HTTPError as exc:
    print(f"  ! event_runtime rejected the alert: HTTP {exc.code} {exc.read()[:200]!r}")
    sys.exit(1)
except urllib.error.URLError as exc:
    # Refused connection or a hung accept — the likelier failure, since the
    # POST returns in seconds (the investigation runs in the background) so
    # the 300s timeout is not the usual path. Without this it is a traceback.
    print(f"  ! could not reach event_runtime: {exc.reason}")
    print("    Is the stack healthy?  docker compose ps")
    sys.exit(1)

action = result.get("action", "?")
print(f"  -> triage decided: {action}")
print(f"     {result.get('message', '')[:120]}")

if action not in ("investigate", "escalate"):
    # log_only / notify / suppressed are legitimate verdicts, not failures —
    # this is the noise filter declining to spend a model on something it has
    # already answered or judged low signal.
    print(f"\n  Triage returned '{action}' rather than opening an investigation.")
    print("  That is the noise filter working, not a failure: CFOperator declines")
    print("  to spend a model on findings it has already answered or judged low")
    print("  signal. Every such decision is recorded — see the console.")
    if action == "suppressed":
        print("\n  (Suppression usually means an identical finding is still inside")
        print("   the dedupe window. Each run of this demo is tagged uniquely, so")
        print("   if you are seeing this repeatedly something else is matching.)")
    sys.exit(0)

# ── 3. wait for the investigation the agent just started ─────────────────────
print("\n  investigating (a local model takes ~1-3 min)...")

def find_ours(rows):
    """Our investigation: newer than the baseline AND triggered by our summary.

    Taking the newest row instead would attribute someone else's investigation
    to this demo whenever a real alert lands while it runs — and then print an
    attach line pointing at the wrong incident, which is worse than failing.

    The match is exact, or on the run nonce. It is deliberately NOT a prefix
    match: a real investigation whose trigger happens to be a prefix of this
    summary — something as short as "Prometheus target" would do — satisfies
    both the id and the prefix test, and wins. That is the very failure this
    helper exists to prevent, so a loose comparison here reintroduces it.
    """
    marker = f"[demo {run_id}]"
    for row in rows:
        if int(row.get("id") or 0) <= baseline:
            continue
        trigger = (row.get("trigger") or "").strip()
        if trigger and (trigger == summary or marker in trigger):
            return row
    return None

deadline = time.time() + 600
seen = None
while time.time() < deadline:
    try:
        rows = get(f"{AGENT}/api/investigations?limit=10", TOKEN)
    except Exception:
        time.sleep(5); continue
    rows = rows if isinstance(rows, list) else rows.get("investigations", [])
    match = find_ours(rows)
    if match:
        seen = match
        if match.get("outcome") != "in_progress":
            break
    time.sleep(5)

if not seen:
    print("  ! no investigation matching this alert appeared.")
    print("    Check: docker compose logs agent")
    sys.exit(1)

inv_id = seen.get("id")
detail = {}
try:
    detail = get(f"{AGENT}/api/investigations/{inv_id}", TOKEN)
except Exception:
    pass
findings = detail.get("findings") or {}

print(f"\n  investigation #{inv_id} -> {seen.get('outcome')}"
      f"  ({detail.get('tool_calls_count', '?')} tool calls,"
      f" {round(float(detail.get('duration_seconds') or 0))}s)")
if findings.get("provider"):
    print(f"  model: {findings['provider']}")
rec = findings.get("recommendation") or ""
if rec:
    print(f"\n  recommendation: {rec[:300]}")

# ── 4. the point of the whole thing ──────────────────────────────────────────
print("\n" + "=" * 68)
print("  Take over in your terminal, already briefed on this investigation:")
print(f"\n      cfassist attach {inv_id}\n")
print("  (install: see docs/cockpit.md — the same line CFOperator puts on every")
print("   Slack/Discord/ntfy notification, so an alert is one paste from context)")
print("=" * 68)
PY
