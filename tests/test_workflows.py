"""CI workflow hygiene (CFOP-114).

Two rules, each learned the hard way or nearly so:

1. Tools come from a pinned release URL with a checksum, or are already on
   the runner — never from a third party's install script. The deploy bump
   job used to pipe kustomize's upstream ``hack/install_kustomize.sh`` to
   bash; that script resolves the version through ``api.github.com``
   anonymously, GitHub-hosted runners share egress IPs that trip the
   anonymous rate limit, and the script reports it as "Version v5.4.3 does
   not exist" (2026-08-28, run 33135088528 — the CFOP-113 deploy stalled).
   The repo's own ``scripts/install-cfassist.sh`` is exempt: the release
   workflow pipes it to sh on purpose, to prove that path works.

2. No job that a ``pull_request`` can trigger runs on a self-hosted runner.
   cfoperator is public; a fork PR can edit the workflow it runs under, and
   the homelab runner's user is in the docker group on a box that also runs
   k3s. GitHub's own guidance says the same; this makes it a test failure
   rather than a code review catch.

   One shape is exempt, and only as a whole (CFOP-236, the local-LLM
   review, which has to run where Ollama is): ``pull_request_target`` as the
   only trigger, so the workflow and scripts come from the base branch and a
   PR cannot change what runs; a same-repo ``if``, so forks never start it;
   checkouts of the base only, keeping no credential; nothing that names
   the PR's head; and a token that can at most comment. Drop any one piece
   and the exemption no longer holds, so the test checks every piece.
"""
from repo_paths import REPO_ROOT
import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))

#: This repository, as workflows spell it. A script fetched from here and piped
#: to a shell is the repo's own (install-cfassist.sh, exercised on purpose).
FIRST_PARTY = re.compile(r"\$\{\{ ?github\.repository ?\}\}|\$\{GITHUB_REPOSITORY\}|aachtenberg/cfoperator")
#: A raw.githubusercontent.com URL that is not this repository's own file.
THIRD_PARTY_RAW = re.compile(
    r"raw\.githubusercontent\.com/"
    r"(?!(\$\{\{ ?github\.repository ?\}\}|\$\{GITHUB_REPOSITORY\}|aachtenberg/cfoperator)/)")
#: Anything piped into a shell: `| bash`, `| sh -s 1.2`, `| sudo bash`.
PIPE_TO_SHELL = re.compile(r"\|\s*(sudo\s+(-E\s+)?)?(ba|da|z)?sh\b")


def read(wf):
    return wf.read_text(encoding="utf-8")


def code(text):
    """The workflow without its comment lines: what a step runs, not what the
    comment beside it explains (the comments name the old script on purpose)."""
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def triggers(doc):
    # PyYAML reads a bare `on:` key as boolean True.
    on = doc.get("on", doc.get(True, {}))
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return set(on)
    return set(on or {})


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_pipes_a_third_party_script_to_a_shell(wf):
    """The class: `curl … | bash` of anything that is not this repo's own
    script. get.helm.sh, install_kustomize.sh, a rustup one-liner — each is
    an unpinned fetch executed as the runner, and the kustomize one also
    asked api.github.com for its version. Pinned tarballs with a sha256, or
    tools the runner already carries, do not need a shell pipe."""
    for line in code(read(wf)).splitlines():
        if PIPE_TO_SHELL.search(line) and not FIRST_PARTY.search(line):
            pytest.fail(f"{wf.name} pipes a third-party script to a shell: {line.strip()!r}")


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_resolves_a_tool_through_the_github_api(wf):
    """The other half of the kustomize failure: an anonymous api.github.com
    lookup from a hosted runner's shared IP is rate-limited at random. Release
    assets have stable download URLs; use those."""
    text = code(read(wf))
    assert "api.github.com" not in text, f"{wf.name} calls api.github.com from a step"


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_fetches_a_third_party_raw_file(wf):
    text = code(read(wf))
    m = THIRD_PARTY_RAW.search(text)
    assert m is None, (
        f"{wf.name} fetches {text[m.start():m.start() + 90]!r} — pin the release "
        "tarball from releases/download/ with a sha256 instead")


def test_the_bump_job_pins_kustomize_by_release_url_and_checksum():
    text = code(read(REPO_ROOT / ".github" / "workflows" / "build-cfoperator-main.yml"))
    job = text[text.index("bump-deploy-repo:"):]
    assert "releases/download/kustomize%2Fv5.4.3/kustomize_v5.4.3_linux_amd64.tar.gz" in job
    assert "sha256sum -c" in job, "the kustomize tarball is not checksummed"
    assert "install_kustomize.sh" not in job
    # The happy path verifies the binary it is about to run, not PATH.
    assert 'KUSTOMIZE_SHA256  /usr/local/bin/kustomize" | sha256sum -c' in job
    assert '"$KUSTOMIZE" edit set image' in job


def test_the_bump_job_leaves_no_credential_on_the_persistent_runner():
    """actions/checkout persists the token into .git by default; on
    ubuntu-latest the VM dies with the job, on itx-01 the work dir survives.
    The token rides each git command as a header instead, and the checkout
    is removed whichever way the job ended."""
    text = code(read(REPO_ROOT / ".github" / "workflows" / "build-cfoperator-main.yml"))
    job = text[text.index("bump-deploy-repo:"):]
    assert "persist-credentials: false" in job
    assert ".extraheader=$auth" in job and "DEPLOY_PAT" in job
    assert "if: always()" in job and "rm -rf cfoperator-deploy" in job


def runs_on_of(wf, name, job):
    """A literal runs-on. An expression (`${{ matrix.runner }}`) cannot be
    judged here, so it is refused: resolve it statically."""
    runs_on = job.get("runs-on")
    assert "${{" not in str(runs_on), (
        f"{wf.name} job {name!r} has runs-on: {runs_on!r} — an expression; spell the runner out")
    return str(runs_on).lower()


#: The condition that keeps fork PRs from starting a fenced job at all.
SAME_REPO = "github.event.pull_request.head.repo.full_name == github.repository"
#: Anything that points a step at the PR's own code instead of the base.
PR_HEAD = re.compile(r"pull_request\.head\.(sha|ref)|github\.head_ref|refs/pull/")
#: The only scopes a fenced job may write to: it posts a review comment.
COMMENT_SCOPES = {"issues", "pull-requests"}
#: An allowlist, not a blocklist: any other action is code the fence has not
#: judged, running on the homelab runner.
FENCE_ACTIONS = {"actions/checkout@v4"}
#: Fetching or checking out code from a run step, however the ref is named
#: (`gh pr checkout "$N"` never mentions the head).
FETCHES_CODE = re.compile(
    r"\b(gh\s+pr\s+checkout|git\s+(fetch|checkout|pull|clone|switch|worktree)|tarball|zipball)\b"
    r"|codeload\.github\.com")


def fence_violations(doc, job):
    """Why a PR-reachable self-hosted job is NOT the exempt review shape.
    Empty means every piece of the fence is present (see the module doc)."""
    problems = []
    if "uses" in job:
        problems.append(f"the job calls a reusable workflow ({job['uses']!r}) the fence cannot see into")
    if triggers(doc) != {"pull_request_target"}:
        problems.append(f"triggers are {sorted(triggers(doc))}, want only pull_request_target")
    cond = str(job.get("if", ""))
    if SAME_REPO not in cond or "||" in cond:
        problems.append(f"if: {cond!r} does not require a same-repo PR (and nothing else may OR it away)")
    perms = job.get("permissions")
    if not isinstance(perms, dict):
        problems.append(f"permissions: {perms!r}, want an explicit per-scope map")
    else:
        for scope, level in perms.items():
            if level == "write" and scope not in COMMENT_SCOPES:
                problems.append(f"permissions grant {scope}: write")
    for step in job.get("steps") or []:
        opts = step.get("with") or {}
        if "uses" in step and step["uses"] not in FENCE_ACTIONS:
            problems.append(f"step uses {step['uses']!r}; only {sorted(FENCE_ACTIONS)} are allowed")
        if FETCHES_CODE.search(str(step.get("run", ""))):
            problems.append(f"step {step.get('name', step)!r} fetches or checks out code")
        if "actions/checkout" in str(step.get("uses", "")):
            if "ref" in opts or "repository" in opts:
                problems.append("a checkout names a ref or repository; only the base may be checked out")
            if opts.get("persist-credentials") is not False:
                problems.append("a checkout keeps its credential on the persistent runner")
    # The whole job, not just run/with: a head SHA passed through a job or
    # step env and then used by `run` is the same checkout by another name.
    # `if` is left out; it names pull_request.head.repo, which is the fence.
    # Plus the workflow-level env and defaults, which every step inherits.
    rest = {k: v for k, v in job.items() if k != "if"}
    inherited = {k: doc.get(k) for k in ("env", "defaults") if doc.get(k)}
    if PR_HEAD.search(yaml.safe_dump(rest) + yaml.safe_dump(inherited)):
        problems.append("the job refers to the PR's head (run, with, env or elsewhere)")
    return problems


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_no_pull_request_job_targets_a_self_hosted_runner(wf):
    doc = yaml.safe_load(read(wf))
    if not triggers(doc) & {"pull_request", "pull_request_target"}:
        pytest.skip(f"{wf.name} is not triggered by pull requests")
    for name, job in (doc.get("jobs") or {}).items():
        if "self-hosted" not in runs_on_of(wf, name, job):
            continue
        problems = fence_violations(doc, job)
        assert not problems, (
            f"{wf.name} job {name!r} runs on {job.get('runs-on')!r} and a pull request can "
            "trigger it, which is allowed only in the fenced pull_request_target shape: "
            + "; ".join(problems))


LOCAL_REVIEW = REPO_ROOT / ".github" / "workflows" / "local-llm-review.yml"


def _review_job():
    doc = yaml.safe_load(read(LOCAL_REVIEW))
    return doc, doc["jobs"]["local-review"]


def test_the_local_review_is_the_fenced_shape():
    """Not vacuous: the review really is self-hosted and PR-reachable, so the
    exemption above is exercised by a real workflow, and it passes the fence."""
    doc, job = _review_job()
    assert "self-hosted" in runs_on_of(LOCAL_REVIEW, "local-review", job)
    assert triggers(doc) & {"pull_request_target"}
    assert fence_violations(doc, job) == []


def _drop_same_repo(doc, job):
    job["if"] = "github.event.pull_request.draft == false"

def _or_it_away(doc, job):
    job["if"] += " || github.actor == 'anyone'"

def _add_pull_request(doc, job):
    doc[True if True in doc else "on"]["pull_request"] = {"types": ["opened"]}

def _checkout_head(doc, job):
    job["steps"][0]["with"]["ref"] = "${{ github.event.pull_request.head.sha }}"

def _keep_credential(doc, job):
    job["steps"][0]["with"].pop("persist-credentials")

def _fetch_head_in_a_step(doc, job):
    job["steps"][1]["run"] = "git fetch origin refs/pull/1/head && " + job["steps"][1]["run"]

def _widen_token(doc, job):
    job["permissions"]["contents"] = "write"

def _smuggle_head_through_env(doc, job):
    job.setdefault("env", {})["HEAD"] = "${{ github.event.pull_request.head.sha }}"
    job["steps"][1]["run"] = 'git fetch origin "$HEAD" && ' + job["steps"][1]["run"]


def _use_another_action(doc, job):
    job["steps"].insert(1, {"uses": "someone/setup-thing@v1"})

def _check_out_the_pr_by_number(doc, job):
    job["steps"][1]["run"] = 'gh pr checkout "$PR_NUMBER" && ' + job["steps"][1]["run"]

def _download_a_tarball(doc, job):
    job["steps"][1]["run"] = 'curl -sL "$API/repos/$R/tarball/$SHA" | tar xz && ' + job["steps"][1]["run"]

def _head_in_workflow_env(doc, job):
    doc["env"] = {"HEAD": "${{ github.event.pull_request.head.sha }}"}

def _call_a_reusable_workflow(doc, job):
    job["uses"] = "./.github/workflows/other.yml"


@pytest.mark.parametrize("mutate", [
    _drop_same_repo, _or_it_away, _add_pull_request, _checkout_head,
    _keep_credential, _fetch_head_in_a_step, _widen_token, _smuggle_head_through_env,
    _use_another_action, _check_out_the_pr_by_number, _download_a_tarball,
    _head_in_workflow_env, _call_a_reusable_workflow,
], ids=lambda f: f.__name__.strip("_"))
def test_breaking_any_piece_of_the_fence_fails_it(mutate):
    """The mutation check, kept in the suite: each piece the module doc names
    is load-bearing, so removing any one must be caught."""
    doc, job = _review_job()
    mutate(doc, job)
    assert fence_violations(doc, job), f"{mutate.__name__} went unnoticed"


def test_a_self_hosted_job_lives_only_in_a_main_push_workflow():
    """The inverse, pinned to what actually makes it safe: the workflow has
    no pull_request trigger AND its push trigger is branches: [main] — a bare
    `on: push` would run every branch anyone with write access pushes, and
    that is the whole fleet's deploy token. Tags and workflow_dispatch are
    collaborator-only and stay allowed. The fenced review shape is the one
    other way onto the runner, judged by the test above."""
    for wf in WORKFLOWS:
        doc = yaml.safe_load(read(wf))
        for name, job in (doc.get("jobs") or {}).items():
            if "self-hosted" not in runs_on_of(wf, name, job):
                continue
            if not fence_violations(doc, job):
                continue
            trig = triggers(doc)
            assert not (trig & {"pull_request", "pull_request_target"}), (wf.name, name)
            on = doc.get("on", doc.get(True, {}))
            push = (on or {}).get("push") if isinstance(on, dict) else None
            assert isinstance(push, dict) and push.get("branches") == ["main"], (
                f"{wf.name} job {name!r} is self-hosted but the workflow's push trigger is "
                f"{push!r}; want branches: [main]")


def test_no_test_files_sit_at_the_repo_root():
    """Suites live in ``tests/``, which CI runs as a directory (CFOP-155).

    This inverts CFOP-139. That guard existed because the root-level suite was
    an explicit list in ``tests.yml`` and a forgotten entry was worse than a
    missing file: green locally, invisible in CI, surfacing later as a
    regression nobody could explain. Moving the suites into ``tests/`` let CI
    collect a directory instead of a list, which removes the failure mode
    rather than policing it.

    What has to be guarded now is the layout itself. A new ``test_*.py`` at the
    root would not be collected by ``pytest tests`` and would land in exactly
    the same hole the old list dug -- so fail here, with the fix, instead.

    Note this cannot be written as the old assertion was. Once the files moved,
    ``REPO_ROOT.glob("test_*.py")`` matches nothing, so a check phrased as
    "every match is registered" passes vacuously forever. Asserting the glob is
    *empty* is the form that still means something after the move.
    """
    stray = sorted(p.name for p in REPO_ROOT.glob("test_*.py"))
    assert not stray, (
        "test files found at the repo root — CI runs `pytest tests`, so these "
        "would never execute there:\n  " + "\n  ".join(stray)
        + "\n\nMove them into tests/ and take REPO_ROOT from "
          "`from repo_paths import REPO_ROOT` rather than Path(__file__).parent.")


def test_ci_collects_the_tests_directory_rather_than_a_file_list():
    """The suite-registration hole stays closed only while CI globs.

    If someone reverts ``tests.yml`` to naming files, the guard above keeps
    passing (the root stays clean) while unlisted files under ``tests/`` go
    silently uncollected — the CFOP-139 failure mode, one directory deeper.
    """
    ci = (REPO_ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert re.search(r"^\s*run\s+\"tests\"\s", ci, re.M), (
        "tests.yml no longer runs the tests/ directory as a unit. If the "
        "root-level invocation went back to an explicit file list, an "
        "unregistered suite silently never runs in CI (CFOP-139).")


def test_every_test_directory_runs_in_ci():
    """A directory that ships tests beside its code must be in tests.yml's loop.

    The suite cannot run as one flat ``pytest`` (same-named top-level modules),
    so each directory is its own invocation in a hand-maintained list — and a
    new service (``tracker/``, CFOP-170) whose tests are green locally and
    never run in CI looks exactly like one that is guarded. ``tests/``,
    ``observability`` and ``auth`` are collected by their own lines.
    """
    wf = (REPO_ROOT / ".github" / "workflows" / "tests.yml").read_text()
    m = re.search(r"for d in ([^;]+); do", wf)
    assert m, "tests.yml no longer loops over directories; update this guard"
    listed = set(m.group(1).split())
    # scripts/test_model_local.py is a hand-run model harness that collects no
    # tests (PR #231 moved the real suites into tests/); it is not a suite.
    separately = {"tests", "observability", "auth", "scripts"}
    checked = 0
    for d in sorted(p for p in REPO_ROOT.iterdir() if p.is_dir()):
        if d.name.startswith(".") or d.name in separately:
            continue
        has_tests = any(d.glob("test_*.py"))
        has_code = any(f for f in d.glob("*.py") if not f.name.startswith("test_"))
        if not (has_tests and has_code):
            continue
        checked += 1
        assert d.name in listed, f"{d.name}/ has test_*.py but tests.yml never runs them"
    assert checked >= 5, "the glob found almost nothing; is REPO_ROOT right?"


def test_the_pr_review_posts_in_this_session():
    """A ready pull request is reviewed without anyone commenting @claude.

    The code-review plugin launches background subagents and then ends the
    turn to wait for a notification. This action never resumes that session,
    so the check goes green and the PR gets nothing (claude-code-action
    #1087). @claude still posts, because that workflow has no plugin and
    finishes in the same turn — which is why a mention worked and the
    automatic run did not. The review prompt has to do the work itself and
    post before it stops, and Agent/Task/Skill stay off the allowlist
    because those are the tools that background it.
    """
    text = (REPO_ROOT / ".github" / "workflows" / "claude-code-review.yml").read_text(encoding="utf-8")
    assert "types: [opened, synchronize, ready_for_review, reopened]" in text
    assert "github.event.pull_request.draft == false" in text
    assert "code-review@claude-code-plugins" not in text
    assert "/code-review:code-review" not in text
    assert "Do not launch a subagent" in text
    assert "gh pr comment" in text
    # The allowlist is one line. A tool named there is offered; the plugin
    # path put Agent and Task on it, and that is the run that posted nothing.
    allow = next(line for line in text.splitlines() if "allowedTools" in line)
    for banned in ("Agent", "Task", "Skill"):
        assert banned not in allow, f"{banned} is back on the review allowlist"
    assert "Bash(gh pr comment:*)" in allow
    assert "issues: write" in text
