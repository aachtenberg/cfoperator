"""CLI routing: `attach` became a real verb without breaking the old shapes.

cfassist was a bare command taking a free-form question. Turning it into a
click Group is exactly the change that silently breaks `cfassist "why is the
pod down"` with "No such command 'why'", so every historical invocation gets a
test here — that is the regression class, not the wording of any one output.
"""

import pytest
from click.testing import CliRunner

from cfassist import cli
from cfassist.briefing import ATTACH_VERB
from cfassist.cfoperator import CFOperatorError


@pytest.fixture
def chat_calls(monkeypatch):
    """Run the real `chat` callback with its collaborators stubbed out.

    Deliberately not a replacement for `chat.callback`: the routing being
    tested (option merging, pipe/one-shot/TUI dispatch) lives *inside* that
    callback, so stubbing it would leave the interesting half untested. Only
    `_prepare` (which would otherwise write to $HOME) and the LLM plumbing are
    replaced. Recorded entries describe what actually reached the session.
    """
    calls = []

    def fake_prepare(config_path, model, url):
        calls.append({"config_path": config_path, "model": model, "url": url})
        return ({"llm": {"provider": "ollama", "model": model or "m"},
                 "memory": {}}, "SYSTEM", 0)

    monkeypatch.setattr(cli, "_prepare", fake_prepare)
    monkeypatch.setattr(cli, "LLMClient", lambda cfg: _FakeLLM())
    monkeypatch.setattr(cli, "ToolRegistry", lambda cfg: object())
    monkeypatch.setattr(
        cli, "_run_tui",
        lambda *a, **kw: calls[-1].update(mode="repl") if calls else None)
    monkeypatch.setattr(
        cli, "_run_turn",
        lambda client, tools, display, messages, system_prompt, user_input:
            (calls[-1].update(mode="one-shot", question=user_input), {})[1])
    monkeypatch.setattr(cli, "_save_and_cleanup", lambda cfg, msgs: None)
    return calls


@pytest.fixture
def runner():
    return CliRunner()


# ---- the shapes that already existed --------------------------------------

def test_free_form_question_still_routes_to_chat(runner, chat_calls):
    result = runner.invoke(cli.main, ["why", "is", "the", "pod", "down"])
    assert result.exit_code == 0, result.output
    assert chat_calls[0]["mode"] == "one-shot"
    assert chat_calls[0]["question"] == "why is the pod down"


def test_single_quoted_question_still_routes_to_chat(runner, chat_calls):
    result = runner.invoke(cli.main, ["why is the pod down"])
    assert result.exit_code == 0, result.output
    assert chat_calls[0]["question"] == "why is the pod down"


def test_bare_invocation_still_opens_the_repl(runner, chat_calls):
    result = runner.invoke(cli.main, [])
    assert result.exit_code == 0, result.output
    assert chat_calls[0]["mode"] == "repl"


def test_group_level_options_still_reach_chat(runner, chat_calls):
    """`cfassist --model m "q"` was valid before the group existed."""
    result = runner.invoke(cli.main, ["--model", "gemma4:26b", "what", "broke"])
    assert result.exit_code == 0, result.output
    assert chat_calls[0]["model"] == "gemma4:26b"
    assert chat_calls[0]["question"] == "what broke"


def test_subcommand_level_options_also_work(runner, chat_calls):
    result = runner.invoke(cli.main, ["chat", "what broke", "--model", "qwen"])
    assert result.exit_code == 0, result.output
    assert chat_calls[0]["model"] == "qwen"


def test_subcommand_option_wins_over_the_group(runner, chat_calls):
    result = runner.invoke(
        cli.main, ["--model", "group", "chat", "q", "--model", "sub"])
    assert result.exit_code == 0, result.output
    assert chat_calls[0]["model"] == "sub"


def test_version_flag_still_works(runner):
    result = runner.invoke(cli.main, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("cfassist ")


def test_a_question_that_begins_with_the_verb_is_not_hijacked(runner, chat_calls):
    """`attach` is decided by position, not by sniffing text — but a question
    whose *first word* is the verb is genuinely ambiguous. The resolution is
    documented here rather than left to chance: the verb wins, and an argument
    that is not an id produces a clear error instead of a silent chat turn."""
    result = runner.invoke(cli.main, ["attach", "the log to the ticket"])
    assert result.exit_code == 2
    assert chat_calls == [], "a bad reference must fail before any config work"


# ---- the new verb ---------------------------------------------------------

def test_attach_is_a_registered_subcommand():
    assert ATTACH_VERB in cli.main.commands


def test_attach_prints_the_briefing_and_starts_no_session(runner, monkeypatch):
    seen = {}

    def fake_prepare(config_path, model, url):
        return ({"llm": {"provider": "ollama", "model": "m"}}, "SYSTEM", 0)

    def fake_fetch(config, ref):
        seen["ref"] = ref
        return "THE BRIEFING"

    monkeypatch.setattr(cli, "_prepare", fake_prepare)
    monkeypatch.setattr(cli, "fetch_briefing", fake_fetch)
    monkeypatch.setattr(cli, "LLMClient", _explode("LLMClient"))
    monkeypatch.setattr(cli, "_run_tui", _explode("_run_tui"))

    result = runner.invoke(cli.main, [ATTACH_VERB, "1889", "--print"])
    assert result.exit_code == 0, result.output
    assert "THE BRIEFING" in result.output
    assert seen["ref"] == "1889"


def test_attach_reports_a_bad_reference_without_touching_the_network(runner, monkeypatch):
    monkeypatch.setattr(cli, "_prepare", lambda *a: ({}, "SYSTEM", 0))
    monkeypatch.setattr(cli, "LLMClient", _explode("LLMClient"))

    result = runner.invoke(cli.main, [ATTACH_VERB, "not-an-id", "--print"])
    assert result.exit_code == 2
    assert "cfassist attach <investigation-id>" in result.output


def test_attach_surfaces_the_api_error_hint(runner, monkeypatch):
    monkeypatch.setattr(cli, "_prepare", lambda *a: ({}, "SYSTEM", 0))
    monkeypatch.setattr(cli, "LLMClient", _explode("LLMClient"))

    def boom(config, ref):
        raise CFOperatorError("No CFOperator API token configured",
                              hint="export CFOP_API_TOKEN=…")

    monkeypatch.setattr(cli, "fetch_briefing", boom)
    result = runner.invoke(cli.main, [ATTACH_VERB, "1889"])
    assert result.exit_code == 1
    assert "No CFOperator API token configured" in result.output
    assert "CFOP_API_TOKEN" in result.output


def test_attach_seeds_the_briefing_into_the_system_prompt(runner, monkeypatch):
    """The point of the whole feature: the model starts already knowing."""
    captured = {}

    monkeypatch.setattr(cli, "_prepare",
                        lambda *a: ({"llm": {"provider": "ollama", "model": "m"}},
                                    "BASE PROMPT", 0))
    monkeypatch.setattr(cli, "fetch_briefing", lambda c, r: "THE BRIEFING")
    monkeypatch.setattr(cli, "LLMClient", lambda cfg: _FakeLLM())
    monkeypatch.setattr(cli, "ToolRegistry", lambda cfg: object())

    def fake_tui(config, client, tools, system_prompt, context_count, preamble=None):
        captured["system_prompt"] = system_prompt
        captured["preamble"] = preamble

    monkeypatch.setattr(cli, "_run_tui", fake_tui)

    result = runner.invoke(cli.main, [ATTACH_VERB, "1889"])
    assert result.exit_code == 0, result.output
    assert "BASE PROMPT" in captured["system_prompt"]
    assert "THE BRIEFING" in captured["system_prompt"]
    assert "read-only access to CFOperator" in captured["system_prompt"]
    assert captured["preamble"] == "THE BRIEFING"


def test_attach_with_a_question_runs_one_shot(runner, monkeypatch):
    turns = []

    monkeypatch.setattr(cli, "_prepare",
                        lambda *a: ({"llm": {"provider": "ollama", "model": "m"},
                                     "memory": {}}, "BASE", 0))
    monkeypatch.setattr(cli, "fetch_briefing", lambda c, r: "THE BRIEFING")
    monkeypatch.setattr(cli, "LLMClient", lambda cfg: _FakeLLM())
    monkeypatch.setattr(cli, "ToolRegistry", lambda cfg: object())
    monkeypatch.setattr(cli, "_run_tui", _explode("_run_tui"))
    monkeypatch.setattr(
        cli, "_run_turn",
        lambda client, tools, display, messages, system_prompt, user_input:
            turns.append((system_prompt, user_input)) or {})

    result = runner.invoke(cli.main, [ATTACH_VERB, "1889", "what", "changed?"])
    assert result.exit_code == 0, result.output
    assert len(turns) == 1
    assert turns[0][1] == "what changed?"
    assert "THE BRIEFING" in turns[0][0]


# ---- helpers --------------------------------------------------------------

def _explode(name):
    def _boom(*args, **kwargs):
        raise AssertionError(f"{name} must not be reached in this path")
    return _boom


class _FakeLLM:
    def check_connection(self):
        return True, None

    def close(self):
        pass
