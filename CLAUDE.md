# Working notes for Claude

Project context lives in `docs/`. This file is about *how work gets done here* —
the loop from a backlog item to a merged PR, and the handful of local gotchas
that otherwise cost half an hour each time.

## Issue tracking: Plane

Work is tracked in Plane, not GitHub Issues. Reached over the `plane` MCP
server.

- Workspace project: **cfoperator**, identifier **CFOP** (so issues read `CFOP-12`)
- States: Backlog → Todo → In Progress → Done (plus Cancelled)

**There is no project-wide "list issues" tool.** `get_projects` and
`list_states` work, but to read issues you go one at a time via
`get_issue_using_readable_identifier` with `project_identifier: "CFOP"` and a
sequence number. To find what is in a given state, walk `CFOP-1`, `CFOP-2`, …
until it 404s. Worth knowing before concluding the server is broken.

## The loop

### 1. Verify the issue's premise before planning against it

Read the issue, then check its claims against the code. Issues here are written
carefully and are still sometimes wrong — usually because the codebase moved
after the issue was filed, or because a true observation led to a false
conclusion.

Two from the CFOP-12/13 round, both of which would have produced pointless work:

- CFOP-13 said the console header scrolls away, citing that `index.html` never
  sets `position: sticky`. True, and irrelevant: four of the five pages already
  set it, and `index.html` is an app shell (`body` is `height:100vh` with
  `overflow:hidden`, `header` is `flex-shrink:0`, panes scroll individually) so
  its header cannot leave the viewport. Half the issue evaporated.
- CFOP-8 asked for the Users nav entry to be hidden from members. The route
  comment in `web_server.py` records the opposite decision *and its reason* —
  members use that page to change their own password. **The code's documented
  decision wins over older issue text.** If the code explains itself, believe it.

When a premise does not hold, say so and adjust scope — do not quietly build the
thing anyway, and do not quietly skip it either.

### 2. Write the plan into the Plane issue

Update `description_html` with the plan before writing code, and move the issue
to In Progress. The issue is the durable record; a plan that lives only in a chat
transcript is lost. Keep the original text and append the plan under a dated
heading — CFOP-10 is the reference for the level of detail that has proven
useful (what was decided, what was rejected, and *why*, including the reasoning
that turned out to be wrong).

### 3. Branch and commit

One commit per issue, even when several land in the same PR. Reference the issue
in the trailer as `Refs CFOP-N`.

Commit bodies carry the reasoning, not a restatement of the diff. Anything that
narrows or reinterprets what the issue asked for belongs here explicitly.

### 4. Test before pushing

See below. Run at least the suites your change touches, plus the new ones.

Suites that are not part of a package live in **`tests/`**, and CI collects that
directory as a unit — so a new file there runs in CI with no registration step.
Do not add `test_*.py` at the repo root; `tests/test_workflows.py` fails if you
do, because `pytest tests` would never collect it.

Inside `tests/`, take the repository root from `from repo_paths import
REPO_ROOT`, never `Path(__file__).parent` — that now resolves to `tests/`. It
matters more than it looks: many of these suites work by globbing a tree
(`ui/*.html`, `charts/`, `.github/workflows/`), and a wrong root makes the glob
empty, the loop body unreachable and the assertion vacuous. `repo_paths` checks
the root against sentinel files and raises rather than hand back a bad one.

Prefer a test that guards the *class* of regression over one that pins the
current output. `test_console_nav.py` exists to catch a future page skipping the
shared header, not to assert today's markup. Mutation-check a new guard once
(break the thing on purpose, confirm it fails) so you know it is not vacuous.

### 5. PR

- Include a section for any deviation from what the issues asked for, with the
  reasoning. Reviewers should never have to discover a narrowed scope by reading
  the diff.
- Request a Copilot review.
- Subscribe to PR activity and drive it to green — a CI failure on your own PR
  ends with either a pushed fix or a reply explaining the blocker.
- When reviewing a PR (`/review`), post findings as GitHub review comments on
  the PR. Do not only report them in chat.

### 6. Close the loop in Plane

Move the issue to Done when the PR merges, and record anything learned during
implementation back into the issue if it contradicts the plan.

## Running tests

Install `requirements.txt` plus `pytest`. Missing runtime deps here surface as
confusing failures rather than clean skips — `prometheus-client` missing makes
`event_runtime.telemetry` drop `COMPLETION_REQUESTS` and four
`test_http_investigate.py` tests fail on import, which looks like a real
regression. If something fails in a way that seems unrelated to your change,
check the dep before chasing it.

**The suite cannot run as one flat `pytest`.** Several trees ship a top-level
module of the same name (`nodeaction`, `entrypoint`, `server`), and most
directories use bare imports needing their own directory on `sys.path`, so a
single collection run collides. Each directory is its own invocation with
`PYTHONPATH=<dir>:<repo-root>`. `observability` and `auth` are the exceptions —
repo root only, because putting their directory on the path lets
`observability/docker.py` shadow the real `docker` package and `auth/tokens.py`
shadow anything of that name.

`.github/workflows/tests.yml` is the authoritative list of what runs and how.
Mirror it rather than inventing an invocation.

## Console UI conventions

Five static pages in `ui/`, no build step, no framework, no outbound network
(a CDN reference hangs rather than fails, and the console is what gets opened
when the WAN is down). No exceptions: third-party code is vendored under
`ui/vendor/` through `scripts/vendor_ui.py` and its hashed manifest, and
`test_console_vendor.py` fails on any `http(s)://` in any page.

The header — section nav, active-page state, identity, logout — is shared via
`ui/nav.js`, served from `/nav.js`. **Do not hand-roll a header on a new page.**
Add the mount and the script; `test_console_nav.py` enforces this. The five
pages drifted into three markup shapes and five different link sets when each
carried its own copy.

Pages that need the current user should `await CFOP.me()` rather than fetching
`/api/auth/me` again — `nav.js` already made that call. Since `nav.js` is
deferred, a page's own bootstrap has to run on `DOMContentLoaded`, not inline.

## Deploying

`docs/auth.md` and `docs/DEPLOYMENT.md` are authoritative. One ordering rule
worth repeating because getting it backwards is an outage rather than a degrade:
when a change spans sealed secrets and deployment manifests, **merge the secret
PR first.** It restarts nothing, whereas the manifest PR restarts all four
workloads — and under `strategy: Recreate` the old pod is gone before the new
one starts, so manifests landing against keys that do not exist yet means
`CreateContainerConfigError` with nothing still serving.
