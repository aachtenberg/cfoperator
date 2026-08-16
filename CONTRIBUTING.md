# Contributing

Small project, no ceremony. Issues and PRs both welcome.

## Running the tests — read this first

**`pytest` at the repo root does not work, and the way it fails looks like a
real bug.** Several trees ship a top-level module of the same name
(`nodeaction`, `entrypoint`, `server`), and most directories use bare imports
that need their own directory on `sys.path`, so a single collection run
collides.

Each directory is its own invocation:

```bash
pip install -r requirements.txt pytest

# Per-directory suites: the directory itself, then the repo root
for d in agent tools event_runtime executor changerecord worker mcp_server/tests bridge/tests; do
    PYTHONPATH="$d:$PWD" pytest "$d" -q || break
done

# observability and auth are the exceptions — repo root ONLY.
# Putting their directory on the path lets observability/docker.py shadow the
# real `docker` package, and auth/tokens.py shadow anything of that name.
PYTHONPATH="$PWD" pytest observability auth -q

# Root-level suites are an explicit list, not a glob
PYTHONPATH="$PWD" pytest test_*.py -q
```

`.github/workflows/tests.yml` is the authoritative version of the above. If it
disagrees with this file, it wins — mirror it rather than inventing an
invocation.

**Missing dependencies surface as confusing failures rather than clean skips.**
A missing `prometheus-client` makes `event_runtime.telemetry` drop
`COMPLETION_REQUESTS`, which fails four `test_http_investigate.py` tests on
import and looks exactly like a regression you caused. If something fails in a
way unrelated to your change, check the dependency before chasing it.

If you add a root-level `test_*.py`, **register it in
`.github/workflows/tests.yml`** — that list is explicit, so an unregistered
file silently never runs in CI.

## What makes a good PR here

- **Reasoning in the commit body**, not a restatement of the diff. If you
  narrowed or reinterpreted the ask, say so explicitly — a reviewer shouldn't
  have to discover a changed scope by reading the code.
- **Tests that guard the class of regression**, not today's output.
  `test_console_nav.py` exists to catch a future page skipping the shared
  header, not to assert current markup. If you add a guard, break the thing on
  purpose once to confirm it actually fails.
- **No new outbound network calls** without discussion. See SECURITY.md — the
  no-telemetry promise is a product commitment, and the console UI has no
  outbound network at all (a CDN reference hangs rather than fails).

## The one architectural rule

**The agent proposes; a human disposes.** It may open a pull request. It may not
mutate a running cluster. Any change that moves an action across that line needs
to be a conversation first, however well-gated it looks — that boundary is the
reason the thing is trustworthy enough to run unattended.

## Console UI

Five static pages in `ui/`, no build step, no framework. The header is shared
via `ui/nav.js` — **don't hand-roll one on a new page**, add the mount and the
script (`test_console_nav.py` enforces this). Pages needing the current user
should `await CFOP.me()` rather than re-fetching `/api/auth/me`; since `nav.js`
is deferred, a page's own bootstrap must run on `DOMContentLoaded`.

## Adding an integration

The shipped backend list is short on purpose. Before writing one, open an
**integration request** issue — partly so the work isn't duplicated, mostly
because that issue is the demand signal that decides what gets built. The
adapter walkthrough is in
[docs/infrastructure-config.md](docs/infrastructure-config.md).
