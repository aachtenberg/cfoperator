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
   k3s. The one self-hosted job, ``bump-deploy-repo``, is main-push only.
   GitHub's own guidance says the same; this makes it a test failure
   rather than a code review catch.
"""
import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted((Path(__file__).parent / ".github" / "workflows").glob("*.yml"))

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
    text = code(read(Path(__file__).parent / ".github" / "workflows" / "build-cfoperator-main.yml"))
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
    text = code(read(Path(__file__).parent / ".github" / "workflows" / "build-cfoperator-main.yml"))
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


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_no_pull_request_job_targets_a_self_hosted_runner(wf):
    doc = yaml.safe_load(read(wf))
    if not triggers(doc) & {"pull_request", "pull_request_target"}:
        pytest.skip(f"{wf.name} is not triggered by pull requests")
    for name, job in (doc.get("jobs") or {}).items():
        assert "self-hosted" not in runs_on_of(wf, name, job), (
            f"{wf.name} job {name!r} runs on {job.get('runs-on')!r} and a pull_request can "
            "trigger it — a fork could edit this workflow and run on the homelab runner")


def test_a_self_hosted_job_lives_only_in_a_main_push_workflow():
    """The inverse, pinned to what actually makes it safe: the workflow has
    no pull_request trigger AND its push trigger is branches: [main] — a bare
    `on: push` would run every branch anyone with write access pushes, and
    that is the whole fleet's deploy token. Tags and workflow_dispatch are
    collaborator-only and stay allowed. Today one job; a second has to pass
    the same bar."""
    for wf in WORKFLOWS:
        doc = yaml.safe_load(read(wf))
        for name, job in (doc.get("jobs") or {}).items():
            if "self-hosted" not in runs_on_of(wf, name, job):
                continue
            trig = triggers(doc)
            assert not (trig & {"pull_request", "pull_request_target"}), (wf.name, name)
            on = doc.get("on", doc.get(True, {}))
            push = (on or {}).get("push") if isinstance(on, dict) else None
            assert isinstance(push, dict) and push.get("branches") == ["main"], (
                f"{wf.name} job {name!r} is self-hosted but the workflow's push trigger is "
                f"{push!r}; want branches: [main]")
