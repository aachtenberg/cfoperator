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


def test_the_cockpit_terminal_is_the_operators_own_binary():
    """No service identity in this system holds pods/exec or pods/attach, and
    the cockpit deliberately does not add one at any tier: the operator's
    laptop already has cluster and ssh credentials. If the attach ever moves
    server-side (the CFOP-59 console drawer), that RBAC decision has to be made
    deliberately — and this test is what makes moving it a conscious act rather
    than a refactor.

    The tier-1 argv moved to the server when the ladder made every tier answer
    in one shape (CFOP-36), so the guard follows it. What did not move is the
    part that matters: the client runs argv the agent sent, on the operator's
    machine, and the agent holds no way to open a terminal itself.
    """
    source = GO_SPAWN_CMD.read_text()
    assert 'kubectlBinary = "kubectl"' in source, (
        "the cockpit readiness poll is no longer the operator's own kubectl")
    assert "exec.Command(argv[0], argv[1:]...)" in source, (
        "the attach no longer execs the returned argv directly — a command "
        "string through a shell would make a confused agent a local-execution "
        "primitive on the operator's machine")

    spawner = (REPO_ROOT / "cockpit_spawn.py").read_text()
    assert '"kubectl", "attach", "-it"' in spawner, (
        "tier 1's attach argv is gone from the server that now answers with it")

    rbac = (REPO_ROOT / "charts" / "cfoperator" / "templates" / "rbac.yaml").read_text()
    for subresource in ("pods/exec", "pods/attach"):
        assert subresource not in rbac, (
            f"{subresource} appeared in the chart: some service identity can now "
            "open a shell. That is CFOP-59's decision to make explicitly, not a "
            "side effect of an RBAC edit")


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


# ---- the ladder's how-to (CFOP-36) ------------------------------------------
#
# docs/cockpit.md §5 is a how-to: it tells an operator to run specific commands
# and to expect specific error text. A doc that lies mid-incident is worse than
# no doc, and every one of these strings is produced by code that can be edited
# without anyone opening the doc. So the doc is a contract too.

COCKPIT_DOC = REPO_ROOT / "docs" / "cockpit.md"
LADDER = REPO_ROOT / "cockpit_ladder.py"


def test_the_docs_label_selectors_are_the_labels_the_code_writes():
    """The doc tells you to find your sessions with
    `docker ps --filter label=cfop.dev/role=cockpit` and
    `kubectl get jobs -l cfop.dev/role=cockpit`. Rename the label and those
    commands silently return nothing — which reads as "nothing is running",
    the one answer a janitor doc must never give wrongly."""
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from cockpit_spawn import JOB_ROLE_LABEL, JOB_ROLE_VALUE

    doc = COCKPIT_DOC.read_text(encoding="utf-8")
    selector = f"{JOB_ROLE_LABEL}={JOB_ROLE_VALUE}"
    assert f"docker ps --filter label={selector}" in doc
    assert f"kubectl get jobs -n apps -l {selector}" in doc


def test_the_docs_session_naming_is_the_naming_the_code_uses():
    """"Every artifact is named cfop-cockpit-<id>" is the promise the manual
    cleanup commands in the doc rest on."""
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from cockpit_ladder import session_name

    doc = COCKPIT_DOC.read_text(encoding="utf-8")
    assert session_name(1889) == "cfop-cockpit-1889"
    assert "/tmp/cfop-cockpit-*" in doc
    assert "cfop-cockpit-<investigation-id>" in doc


def test_the_troubleshooting_table_quotes_errors_the_code_can_produce():
    """Each row of "When it does not work" is keyed on text the operator sees.
    Reword a message without the doc and the row becomes unfindable exactly
    when someone is searching for it."""
    doc = COCKPIT_DOC.read_text(encoding="utf-8")
    ladder = LADDER.read_text(encoding="utf-8")
    spawn = (REPO_ROOT / "cockpit_spawn.py").read_text(encoding="utf-8")
    go_spawn = GO_SPAWN_CLIENT.read_text(encoding="utf-8")

    for fragment, source, where in [
        ("is not in infrastructure.hosts", ladder, "cockpit_ladder"),
        ("the affected host could not be probed", ladder, "cockpit_ladder"),
        ("was requested but is not available", ladder, "cockpit_ladder"),
        ("neither docker nor podman is installed", ladder, "cockpit_ladder"),
        ("is the release tagged?", ladder, "cockpit_ladder"),
        ("cockpit concurrency cap reached", spawn, "cockpit_spawn"),
        ("spawning a cockpit is admin-only", go_spawn, "the Go spawn client"),
    ]:
        assert fragment in doc, f"docs/cockpit.md no longer documents {fragment!r}"
        assert fragment in source, (
            f"{where} no longer produces {fragment!r}, but docs/cockpit.md still "
            "tells operators to look for it")


def test_the_documented_console_setting_is_the_one_the_agent_reads():
    """The doc says the janitor interval is changeable live from the console.
    That is only true while the agent reads this exact setting key."""
    doc = COCKPIT_DOC.read_text(encoding="utf-8")
    agent = (REPO_ROOT / "agent" / "agent.py").read_text(encoding="utf-8")
    assert "cockpit_reap_interval" in doc
    assert "get_setting('cockpit_reap_interval'" in agent


def test_the_documented_tier_names_are_the_ones_the_flag_accepts():
    """`--tier pod|container|host|ssh` is copied out of the doc and typed."""
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from cockpit_ladder import VALID_TIERS

    doc = COCKPIT_DOC.read_text(encoding="utf-8")
    assert "--tier pod|container|host|ssh" in doc
    for tier in ("pod", "container", "host", "ssh"):
        assert tier in VALID_TIERS
    help_text = GO_ATTACH_CMD.read_text(encoding="utf-8")
    assert "auto|pod|container|host|ssh" in help_text, (
        "the --tier flag's own help no longer offers what the doc documents")


def test_the_documented_spawn_flags_exist_on_the_command():
    """The how-to tells operators to type these. A flag documented but never
    registered fails with "unknown flag" at the worst possible moment, and the
    doc and the CLI ship separately."""
    help_text = GO_ATTACH_CMD.read_text(encoding="utf-8")
    doc = COCKPIT_DOC.read_text(encoding="utf-8")
    for flag in ("spawn", "tier", "host", "session-ttl"):
        assert f'"{flag}"' in help_text, f"attach no longer registers --{flag}"
        assert f"--{flag}" in doc, f"docs/cockpit.md no longer mentions --{flag}"

    # The *request payload* specifically, not the file: `"host"` also appears
    # as a struct tag on the response, so a whole-file grep passes even when
    # the field has been dropped from the wire. Scoped to the marshalled map.
    client = GO_SPAWN_CLIENT.read_text(encoding="utf-8")
    payload = re.search(r"json\.Marshal\(map\[string\]any\{(.*?)\n\t\}\)",
                        client, re.DOTALL)
    assert payload, "the spawn request payload is no longer a marshalled map literal"
    for field in ('"tier"', '"host"', '"investigation_id"', '"ttl_seconds"'):
        assert field in payload.group(1), (
            f"the spawn request no longer carries {field}; the flag would be "
            "accepted on the command line and silently dropped on the wire")


# ---- write-back (CFOP-37) ---------------------------------------------------

GO_WRITEBACK_CLIENT = GO_ROOT / "internal" / "cfoperator" / "writeback.go"
GO_SUMMARIZE = GO_ROOT / "internal" / "cfoperator" / "summarize.go"


def test_the_write_back_client_cannot_be_bent_into_another_call():
    """Fourth instance of the guard-in-the-transport rule, and the one that
    matters most: this client runs holding a credential, at the end of a
    session, when nobody is watching."""
    source = GO_WRITEBACK_CLIENT.read_text(encoding="utf-8")
    assert "write-back client refuses" in source, (
        "the write-back transport's allowlist is gone")
    for forbidden in ("MethodPut", "MethodPatch", "MethodDelete"):
        assert f"http.{forbidden}," not in source, (
            f"{forbidden} appears in the write-back transport; it may only POST "
            "its own two paths")


def test_the_write_back_endpoints_exist_on_both_sides():
    """The CLI and the agent ship separately, so the two paths write-back POSTs
    to are asserted across the seam — the same rule the spawn path follows."""
    source = GO_WRITEBACK_CLIENT.read_text(encoding="utf-8")
    web = WEB_SERVER.read_text(encoding="utf-8")

    assert 'LearningsPath = "/api/learnings"' in source
    assert "@self.app.route('/api/learnings', methods=['POST'])" in web, (
        "cfassist writes learnings to a route web_server.py no longer registers")

    assert '"/api/investigations/" + strconv.Itoa(investigationID) + "/session"' in source
    assert "'/api/investigations/<int:investigation_id>/session'" in web, (
        "cfassist records sessions on a route web_server.py no longer registers")


def test_write_back_travels_on_the_sessions_own_scope():
    """Both write-back endpoints must take `investigate` — the scope the
    cockpit session token is minted with. Raise either to an admin role and the
    credential that dies with the session can no longer record what the session
    learned, which is the one thing it exists to do."""
    web = WEB_SERVER.read_text(encoding="utf-8")
    for route in ("'/api/learnings', methods=['POST']",
                  "'/api/investigations/<int:investigation_id>/session'"):
        idx = web.index(route)
        following = web[idx:idx + 400]
        assert "@require_token_scope('investigate')" in following, (
            f"the route at {route} no longer accepts the session's own scope")


def test_the_session_outcome_vocabulary_matches_on_both_sides():
    """One client inventing a word is how a vocabulary drifts; the agent 400s
    on anything it does not know, so the two lists have to agree."""
    go_src = GO_WRITEBACK_CLIENT.read_text(encoding="utf-8")
    web = WEB_SERVER.read_text(encoding="utf-8")

    go_block = re.search(r"var SessionOutcomes = \[\]string\{(.*?)\}", go_src, re.DOTALL)
    py_block = re.search(r"_SESSION_OUTCOMES = \((.*?)\)", web, re.DOTALL)
    assert go_block and py_block, "the outcome vocabularies moved"
    go_words = set(re.findall(r'"([a-z_]+)"', go_block.group(1)))
    py_words = set(re.findall(r"'([a-z_]+)'", py_block.group(1)))
    assert go_words == py_words, (
        f"cfassist sends {sorted(go_words)} but the agent accepts {sorted(py_words)}")


def test_the_docs_write_back_promises_are_the_flags_that_exist():
    """docs/cockpit.md §6 tells operators what exit does and how to turn it
    off. A doc that promises a flag the CLI does not register fails at the
    worst possible moment."""
    doc = COCKPIT_DOC.read_text(encoding="utf-8")
    attach = GO_ATTACH_CMD.read_text(encoding="utf-8")
    assert '"no-writeback"' in attach, "attach no longer registers --no-writeback"
    assert "--no-writeback" in doc
    for promise in ("session recorded on investigation",
                    "session not recorded (--no-writeback)",
                    "raw tail"):
        assert promise in doc, f"docs/cockpit.md no longer documents {promise!r}"


def test_write_back_is_opt_out_not_opt_in():
    """A default-off memory feature is one nobody remembers to turn on, and the
    whole issue is that knowledge currently dies with the terminal."""
    attach = GO_ATTACH_CMD.read_text(encoding="utf-8")
    idx = attach.index('"no-writeback"')
    assert "false" in attach[idx:idx + 120], (
        "--no-writeback no longer defaults to false; write-back would be opt-in")
