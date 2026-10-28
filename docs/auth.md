# Console Auth: Users, Roles, and API Tokens

The agent console on `:8083` runs `hostNetwork` on the k3s node, so it is bound
to the LAN interface directly and `/api/remediations/<id>/approve` hands work
straight to the executor. Authentication used to be a single set of credentials
sealed into k3s: one username, one password hash, and one bearer token shared by
`event_runtime`, the Slack bridge, and the MCP server alike.

That shared token is the problem this replaces. It belongs to no one, so the
audit trail cannot say who acted; it cannot be revoked for one caller without
breaking the others; and every holder gets the full console, including approve.

## The model

**Users** are people. Two roles:

| Role | May do |
|------|--------|
| `admin` | Everything: the remediation lifecycle, settings, config reload, on-demand sweeps, pool toggles, and user/token management |
| `member` | Every read, plus chat, Q&A, feedback, and KB search — and manage their own tokens and password |

The dividing line is "changes how the system behaves or what it will act on".
A member can understand the fleet without being able to point the executor at
production.

**Tokens** are for machines. Each one carries its own scopes, in the same tiers
the MCP server already used:

```
read  ⊂  investigate  ⊂  remediate
```

Granting `remediate` implies the other two. A token's scopes are checked
independently of any user session, and a `read` token cannot approve a
remediation no matter who minted it.

Two guards are worth knowing about because they are easy to assume and
expensive to discover missing:

- A member cannot mint a token more powerful than the member. Otherwise token
  creation is a way around the role check.
- Deactivating a user stops their tokens too. A revoked person must not keep a
  working credential.

## Where things live

| Table | Holds |
|-------|-------|
| `auth_users` | username, werkzeug password hash, role, active flag, last login |
| `auth_api_tokens` | label, SHA-256 digest, display prefix, scopes, owner, last used, expiry, revocation |
| `auth_audit` | logins, lockouts, user and token mutations, legacy-token use |

These share the knowledge base's PostgreSQL database. Nothing is stored in
cleartext: a token's secret exists exactly once, in the response to the request
that created it.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `CFOP_AUTH_DB_URL` | derived | Override the auth DSN outright |
| `POSTGRES_HOST` / `_PORT` / `_DB` / `_USER` / `_PASSWORD` | `postgres:5432/cfoperator` | Where auth looks for its database. `KNOWLEDGE_BASE_PG_*` is accepted as a fallback |
| `CFOP_AUTH_DB_DISABLED` | unset | Force legacy single-user mode even if a database is reachable |
| `CFOP_SESSION_SECRET` | — | Signs session cookies; without it, cookies break on restart and across replicas |
| `CFOP_AUTH_DISABLED` | unset | Opens the console entirely. **Local docker-compose only** — it logs a loud warning on every start |
| `CFOP_UI_USERNAME` / `CFOP_UI_PASSWORD_HASH` | — | Legacy credentials, now the bootstrap path for the first admin |
| `CFOP_API_TOKEN` | — | **Retired on the agent 2026-08-09.** Was the legacy shared bearer. Each calling service now mounts its own database token *as* this variable name (see [Service tokens](#service-tokens)); the plain shared key no longer exists. On the agent, leaving it unset is correct — setting it re-enables a shared credential that belongs to no user |

## Service tokens

Each machine caller holds its own token, minted at `/admin?tab=tokens` with the
least scope that works, sealed into `homelab-infra/secrets/.env.secrets`, and
mounted on that service *as* `CFOP_API_TOKEN`, so the callers' code did not
change when the shared token went:

| Service | Scope | Why |
|---------|-------|-----|
| `event_runtime` | `investigate` | calls `/v1/triage` and `/v1/investigate` |
| `bridge` (local runtime) | `investigate` | drives the agent chat API and the `investigate:` enqueue |
| `cfoperator-mcp` | `remediate` | the facade exposes approve, reject, resolve and investigation triage, which are admin-only routes |

Revoking one stops that service alone.

### Checking for shared-credential use

Every use of a shared credential writes an audit row, so "is anything still
using it?" is a question with an answer rather than a guess. **Do not just
count rows** — two different credentials write the same `token.legacy_used`
event:

| Writer | Credential | `source_ip` |
|--------|------------|-------------|
| `web_auth.py` | shared `CFOP_API_TOKEN` on the agent | always set |
| `mcp_server/auth.py` | `CFOP_MCP_TOKEN` (separate credential) | never set |

So filter on `source_ip`, and compare against a timestamp rather than expecting
zero — historical rows do not age out. `/api/auth/audit` is admin-only, so the
check needs an admin session or a token carrying `remediate`. The
`cfoperator-mcp` pod's own token does:

```bash
kubectl -n apps exec deploy/cfoperator-mcp -- python3 -c "
import os,urllib.request,json
r=urllib.request.Request(os.environ['CFOP_AGENT_URL']+'/api/auth/audit?event=token.legacy_used',
    headers={'Authorization':'Bearer '+os.environ['CFOP_API_TOKEN']})
rows=json.load(urllib.request.urlopen(r))['audit']
print(len([e for e in rows if e.get('source_ip') and e['created_at'] > '<since-timestamp>']))"
```

### Removing a key a deployment references

Manifest first, secret second — the reverse of adding one. Adding needs the
secret first because a manifest referencing a missing key is
`CreateContainerConfigError`. Removing inverts it: dropping the key while a
manifest still references it leaves a dangling `secretKeyRef` that only breaks
at the next restart, and under `strategy: Recreate` that restart is an outage.

## Failure modes

**Database unreachable at startup.** The auth store is opened once, when the
agent starts. If that fails, the console falls back to legacy environment
credentials and logs the error — and legacy mode needs `CFOP_UI_USERNAME`,
`CFOP_UI_PASSWORD_HASH` and `CFOP_API_TOKEN` all set. The documented deploy sets
none of them, so every non-exempt route returns 503, only `/api/health` and
`/metrics` answer, and it stays that way until the pod restarts with the
database reachable. This is not a fall-open.

**Database unreachable mid-request.** 503, never 401 and never 2xx. A lookup
that cannot run is an outage, not "no such user" — answering 401 would send a
legitimate user hunting for a password problem that does not exist.

**Nothing configured at all.** Every non-exempt route returns 503. `/api/health`
and `/metrics` stay open so the pod remains live and diagnosable.

## Runbooks

### Locked out — no usable admin

User management is admin-only, so the console cannot fix this from inside
itself. From the agent pod:

```bash
kubectl -n apps exec -it deploy/cfoperator -- python scripts/create_admin.py alice
kubectl -n apps exec -it deploy/cfoperator -- python scripts/create_admin.py alice --reset
```

`--reset` also reactivates the account and restores the role, since a
deactivated or demoted admin is exactly the state that sends you here.

Failing that: delete every row from `auth_users` and restart the pod with
`CFOP_UI_USERNAME` / `CFOP_UI_PASSWORD_HASH` set. The empty table re-triggers
the bootstrap.

The last active admin cannot be demoted or deactivated through the API, so
reaching this state takes deliberate effort.

### Rotating a service token

Tokens overlap, so there is no gap:

1. Mint the replacement at `/admin?tab=tokens` with the same scopes.
2. Update `.env.secrets`, re-seal, roll the deployment.
3. Confirm the new token's `last_used_at` is advancing and the old one's is not.
4. Revoke the old token. Revocation is immediate — the next request fails.

### Someone left

Deactivate the account at `/admin?tab=users`. Their session stops on the next
request (the account is re-checked per request, not per cookie), and their
tokens stop working at the same moment.

People change their own password and manage their own tokens at `/account`.

## What is deliberately not gated

`/api/health` and `/metrics` stay open — kubelet probes and the Prometheus
scrape, both read-only, both broken by gating for no benefit.

The `/v1/*` endpoints are not role-gated. They are machine callbacks, and a
role check would 403 every one of them and strand remediations mid-flight.
Four of them — `/v1/deep-investigations`, `/v1/remediations/<id>/complete`,
`/v1/remediations/feed-sweeps` and `/v1/remediations/feed-summary` — are called
by executor Jobs and the deep-investigation worker, and authenticate with the
`X-CFOP-Token` completion secret via their own check. That secret is
deliberately honoured on those four paths only: it is injected into every
disposable executor Job pod, so honouring it anywhere else would turn the
lowest-privilege credential in the system into a full console credential.
`/v1/investigate` and `/v1/triage` do not take the completion secret. They sit
behind the ordinary console gate (a bearer token or a session), and
`event_runtime` calls them with its own `investigate` token.

The reverse direction has its own gate. The event runtime's HTTP surface is
guarded by `CFOP_RUNTIME_TOKEN`, a static bearer rather than a database token
because the runtime must keep working with Postgres down. The agent is one of its
callers — it forwards sweep findings to `POST /alert` — so **both** deployments
need the token. Mounting it on the runtime alone refuses the agent, and that
stopped every sweep notification for 34 days (CFOP-214). See
[event-runtime-quickstart.md](event-runtime-quickstart.md).

`/account` and `/admin` are served without a role check. The markup gives
nothing away, every `/api/*` call they make is authorised on its own, and
gating the page would only mean a member sees a 403 instead of a page that
correctly shows them their own tokens (or a banner pointing them at
`/account`). The Admin nav link is filtered client-side for non-admins.
Legacy `/users` and `/tokens` redirect into `/admin`.

## History

Shipped 2026-08-05 (#105) with no flag day, so every step left the previous
state working. On first start the agent created the auth tables and seeded the
first admin from `CFOP_UI_USERNAME` / `CFOP_UI_PASSWORD_HASH`, copying the hash
verbatim so the existing password kept working. Per-person accounts and
per-service tokens followed. The shared `CFOP_API_TOKEN` was retired on
2026-08-09, once the legacy-use count above stayed at zero after the roll, and
the bootstrap pair was then dropped from the agent. That removal was a no-op
rather than a lockout, because bootstrap is skipped once any user exists.
