"""The attach line Slack prints must be a command the shipped binary implements.

CFOP-29's handoff is one paste: an operator reads an alert on their phone, and
the last line of it is `cfassist attach 1889`. Two artifacts have to agree for
that to work, and they are built, tested and deployed separately:

  * ``event_runtime/notifications.py`` runs inside the agent image and *prints*
    the command.
  * ``cfassist-go`` is a single binary cross-compiled from this repo and
    installed on the operator's laptop from a GitHub release; it *implements*
    the command.

Nothing links them at runtime, so a rename on either side is a silent failure
that only shows up mid-incident, at the worst possible moment, as "unknown
command". This module is the link: it reads the verb out of the Go source and
compares it to the string the notifier emits.

Reading source text rather than invoking the binary is deliberate — CI has no
compiled cfassist, and requiring one would make this test skip exactly when it
is most needed. The Go side has the mirror of this assertion in
``cfassist-go/cmd/cfassist/attach_test.go``, which checks the verb is really
registered with cobra rather than merely present in a string.
"""

import re
from pathlib import Path

import pytest

from event_runtime.notifications import ATTACH_COMMAND, ATTACH_HINT_PREFIX

REPO_ROOT = Path(__file__).resolve().parent
GO_ROOT = REPO_ROOT / "cfassist-go"
GO_BRIEFING = GO_ROOT / "internal" / "cfoperator" / "briefing.go"
GO_ATTACH_CMD = GO_ROOT / "cmd" / "cfassist" / "attach.go"
GO_MAIN = GO_ROOT / "cmd" / "cfassist" / "main.go"
# Cockpit tier 1 (CFOP-35): the same split-artifact problem one level up — the
# CLI spawns over an HTTP endpoint the agent registers, and the two ship
# separately, so the path has to be asserted across the seam like the verb is.
GO_SPAWN_CMD = GO_ROOT / "cmd" / "cfassist" / "spawn.go"
GO_SPAWN_CLIENT = GO_ROOT / "internal" / "cfoperator" / "spawn.go"
WEB_SERVER = REPO_ROOT / "web_server.py"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-cfassist.yml"


@pytest.fixture(scope="module")
def go_verb():
    """The verb declared in cfassist-go: `const AttachVerb = "attach"`."""
    source = GO_BRIEFING.read_text()
    match = re.search(r'const\s+AttachVerb\s*=\s*"([^"]+)"', source)
    assert match, f"AttachVerb is no longer declared in {GO_BRIEFING.name}"
    return match.group(1)


@pytest.fixture(scope="module")
def cobra_use():
    """The `Use:` line of the cobra command, i.e. what the binary answers to."""
    source = GO_ATTACH_CMD.read_text()
    match = re.search(r'Use:\s*"([^"]+)"', source)
    assert match, f"no cobra Use: line found in {GO_ATTACH_CMD.name}"
    return match.group(1)


def test_go_source_is_present():
    """Guards the whole module against silently passing on a moved tree: every
    assertion below reads these files, so their absence must fail loudly rather
    than turn the contract into a no-op."""
    for path in (GO_BRIEFING, GO_ATTACH_CMD, GO_MAIN):
        assert path.is_file(), f"{path} is missing — cfassist-go is the product"


def test_notification_verb_matches_the_go_command(go_verb, cobra_use):
    """The core contract.

    Mutation check: change AttachVerb in briefing.go (or the verb inside
    ATTACH_COMMAND) and this goes red.
    """
    rendered = ATTACH_COMMAND.format(investigation_id=1889)
    binary, verb, argument = rendered.split()

    assert verb == go_verb, (
        f"Slack advertises `{verb}` but cfassist-go implements `{go_verb}`"
    )
    assert cobra_use.split()[0] == go_verb, (
        f"cobra registers `{cobra_use.split()[0]}`, AttachVerb says `{go_verb}`"
    )
    assert argument == "1889"


def test_advertised_binary_is_the_one_the_release_builds():
    """`cfassist` in the notification has to be the name of the artifact an
    operator actually installs, or the paste fails at the shell rather than at
    the verb."""
    rendered = ATTACH_COMMAND.format(investigation_id=1)
    binary = rendered.split()[0]
    assert binary == "cfassist"

    workflow = RELEASE_WORKFLOW.read_text()
    assert "go build" in workflow, "release workflow no longer builds the binary"
    assert "-o cfassist-" in workflow, (
        "release artifacts are no longer named cfassist-*; the advertised "
        "command assumes the binary lands on PATH as `cfassist`"
    )


def test_attach_takes_the_investigation_id_as_its_argument(cobra_use):
    """The line pastes an id positionally. If the Go command ever moves it
    behind a flag, the advertised one-liner stops working."""
    assert "<investigation-id>" in cobra_use, (
        f"attach's Use line no longer takes a positional id: {cobra_use!r}"
    )


def test_attach_is_registered_on_the_root_command():
    """A verb that exists but is never added to cobra is not a verb.

    The Go test asserts this properly against the built command tree; here it
    is a cheap guard against the wiring being deleted while attach.go survives.

    Comment lines are stripped first — a commented-out `// rootCmd.AddCommand(
    newAttachCmd())` is exactly the shape this is meant to catch, and a plain
    substring search would happily match it.
    """
    live = "\n".join(
        line for line in GO_MAIN.read_text().splitlines()
        if not line.lstrip().startswith("//")
    )
    assert "AddCommand(newAttachCmd())" in live, (
        "attach is no longer registered on the root command in main.go"
    )


def test_print_flag_exists_for_piping_the_briefing():
    """docs/cockpit.md tells operators to pipe `--print` into other agents."""
    assert '"print"' in GO_ATTACH_CMD.read_text(), (
        "--print disappeared; docs/cockpit.md documents it as the way to drive "
        "a non-cfassist agent"
    )


def test_hint_prefix_leaves_the_command_copyable():
    """The prefix is prose, the command is the payload. Anything that fused the
    two — punctuation, markup — would break the double-click-and-copy."""
    line = ATTACH_HINT_PREFIX + ATTACH_COMMAND.format(investigation_id=1889)
    assert line.endswith("cfassist attach 1889")
    assert not any(ch in line for ch in "`*_<>|")


def test_go_attach_is_read_only_by_construction():
    """CFOP-29 is a read-only handoff. The allowlist lives in the Go transport;
    this asserts it has not quietly grown a mutating method, which is the one
    change that would turn a briefing into an action.
    """
    client_go = (GO_ROOT / "internal" / "cfoperator" / "client.go").read_text()
    match = re.search(
        r"var\s+allowedMethods\s*=\s*map\[string\]bool\{([^}]*)\}", client_go
    )
    assert match, "the read-only method allowlist is gone from client.go"

    body = match.group(1)
    for forbidden in ("MethodPost", "MethodPut", "MethodPatch", "MethodDelete"):
        assert forbidden not in body, (
            f"{forbidden} was added to the attach allowlist; attach must only GET"
        )
    assert "MethodGet" in body


# ---- cockpit spawn: the second cross-artifact seam (CFOP-35) ----------------


def test_spawn_is_a_flag_on_attach_not_a_separate_verb():
    """`cfassist attach 1889 --spawn` is one edit away from the line Slack
    already prints. A separate `cfassist cockpit` verb would mean the
    notification's command could not be turned into a spawn by adding a flag,
    which is the whole ergonomic claim of tier 1."""
    assert '"spawn"' in GO_ATTACH_CMD.read_text(), (
        "--spawn is no longer registered on attach; docs/cockpit.md documents "
        "it as an attach flag")


def test_the_spawn_path_matches_the_route_the_agent_registers():
    """The CFOP-29 verb problem, one level up: the CLI and the agent ship as
    different artifacts, so a renamed route is a silent failure that only
    surfaces mid-incident. Reading both sides is the link.

    Mutation check: change SpawnPath in spawn.go (or the Flask route) and this
    goes red.
    """
    match = re.search(r'const\s+SpawnPath\s*=\s*"([^"]+)"', GO_SPAWN_CLIENT.read_text())
    assert match, f"SpawnPath is no longer declared in {GO_SPAWN_CLIENT.name}"
    go_path = match.group(1)

    routes = re.findall(r"@self\.app\.route\('([^']+)',\s*methods=\['POST'\]\)",
                        WEB_SERVER.read_text(encoding="utf-8"))
    assert go_path in routes, (
        f"cfassist POSTs {go_path} but web_server.py registers no such POST route")


def test_the_cockpit_terminal_is_the_operators_own_kubectl():
    """No service identity in this system holds pods/exec or pods/attach, and
    tier 1 deliberately does not add one: the operator's laptop already has
    cluster credentials. If the attach ever moves server-side (the CFOP-59
    console drawer), that RBAC decision has to be made deliberately — and this
    test is what makes moving it a conscious act rather than a refactor."""
    source = GO_SPAWN_CMD.read_text()
    assert 'kubectlBinary = "kubectl"' in source, (
        "the cockpit TTY is no longer the operator's own kubectl")
    assert '"attach", "-it"' in source, "the interactive attach argv is gone"


def test_the_spawn_client_cannot_be_bent_into_another_call():
    """Same guard-in-the-transport rule as the read-only client above: the
    spawn transport admits one method on one path, checked before the socket
    opens."""
    source = GO_SPAWN_CLIENT.read_text()
    assert "method != http.MethodPost || path != SpawnPath" in source, (
        "the spawn client's one-method-one-path allowlist is gone")
    for forbidden in ("MethodPut", "MethodPatch", "MethodDelete"):
        assert f"http.{forbidden}," not in source, (
            f"{forbidden} appears in the spawn transport; it may only POST its own path")
