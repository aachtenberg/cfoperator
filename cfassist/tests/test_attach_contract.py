"""The Slack line and the CLI verb must stay the same command.

These two halves ship in different artifacts: `event_runtime` runs in the agent
image in the cluster, `cfassist` runs on the operator's laptop. Nothing else
connects them, so a rename on either side produces a notification telling an
operator to paste a command that does not exist — discovered mid-incident,
which is the worst possible time.

This test can see both because CI runs each directory with
``PYTHONPATH=<dir>:<repo-root>``: ``cfassist`` resolves from the first entry,
``event_runtime`` from the second.
"""

from click.testing import CliRunner

from cfassist import cli
from cfassist.briefing import ATTACH_VERB, attach_command
from event_runtime.notifications import ATTACH_COMMAND, _format_message


def test_the_slack_line_and_cfassist_agree_on_the_command():
    """Mutation check: rename ATTACH_VERB, or edit ATTACH_COMMAND, → red."""
    assert ATTACH_COMMAND.format(investigation_id=1889) == attach_command(1889)


def test_the_command_slack_prints_is_one_cfassist_implements():
    """Stronger than string equality: the verb is resolved against the actual
    click group, so deleting the subcommand fails here too."""
    printed = ATTACH_COMMAND.format(investigation_id=1889)
    program, verb, argument = printed.split()
    assert program == "cfassist"
    assert verb in cli.main.commands
    assert verb == ATTACH_VERB
    assert argument == "1889"


def test_the_printed_command_parses_as_a_real_invocation():
    """End to end on the contract: take the exact string Slack renders, split
    it the way a shell would, and feed it to the CLI. A verb that no longer
    accepts a bare id (extra required option, renamed argument) fails here.
    """
    printed = ATTACH_COMMAND.format(investigation_id=1889)
    args = printed.split()[1:]

    result = CliRunner().invoke(cli.main, args + ["--help"])
    assert result.exit_code == 0, result.output
    assert "<investigation-id>" in result.output


def test_a_real_notification_body_ends_with_the_handoff():
    text = _format_message(
        "Action completed: investigate",
        severity="warning",
        details={
            "alert_summary": "Pod immich-kiosk-0 not ready",
            "action": "investigate",
            "result_message": "needs_action",
            "result_details": {"investigation_id": 1889,
                               "provider": "ollama/gemma4:26b"},
        },
    )
    assert text.splitlines()[-1].endswith(attach_command(1889))
