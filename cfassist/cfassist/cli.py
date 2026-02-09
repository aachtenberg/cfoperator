"""CLI entry point — REPL, one-shot, and pipe modes."""

import sys

import click
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.formatted_text import HTML

from cfassist import __version__
from cfassist.config import load_config, ensure_directories, DEFAULT_CONFIG_DIR
from cfassist.client import LLMClient
from cfassist.display import Display
from cfassist.tools import ToolRegistry
from cfassist.streaming import run_conversation
from cfassist.memory import save_conversation, cleanup_old_conversations


def _build_system_prompt(config, context_text=None):
    """Build the system prompt from config and optional context."""
    prompt = config.get("system_prompt", "You are a helpful assistant.")

    if context_text:
        prompt += (
            "\n\n--- Environment Context ---\n"
            "The following files describe the user's environment. "
            "Use this information when answering questions.\n\n"
            + context_text
        )

    return prompt


def _load_context(config):
    """Load context files from the context directory. Returns (text, count)."""
    from cfassist.context import load_context_directory
    ctx_dir = config.get("context", {}).get("directory")
    max_tokens = config.get("context", {}).get("max_tokens", 8000)
    if ctx_dir:
        text, count = load_context_directory(ctx_dir, max_chars=max_tokens * 4)
        return text, count
    return None, 0


def _save_and_cleanup(config, messages):
    """Save conversation to memory and clean up old ones."""
    if messages:
        memory_dir = config.get("memory", {}).get("directory")
        if memory_dir:
            save_conversation(memory_dir, messages)
            max_convos = config.get("memory", {}).get("max_conversations", 50)
            cleanup_old_conversations(memory_dir, max_convos)


def _run_turn(client, tools, display, messages, system_prompt, user_input):
    """Run a single conversation turn."""
    messages.append({"role": "user", "content": user_input})

    result = run_conversation(
        client=client,
        tools=tools,
        display=display,
        messages=messages,
        system_prompt=system_prompt,
    )

    if result.get("response"):
        messages.append({"role": "assistant", "content": result["response"]})

    return result


@click.command()
@click.argument("question", nargs=-1)
@click.option("--config", "config_path", default=None, help="Path to config file")
@click.option("--model", default=None, help="Override LLM model")
@click.option("--url", default=None, help="Override LLM endpoint URL")
@click.option("--version", is_flag=True, help="Show version")
def main(question, config_path, model, url, version):
    """cfassist — CLI assistant for SRE and systems administration.

    Run without arguments for interactive mode.
    Pass a question for one-shot mode.
    Pipe data in for analysis mode.
    """
    if version:
        click.echo(f"cfassist {__version__}")
        return

    # Load config
    config = load_config(config_path)
    ensure_directories(config)

    # Apply CLI overrides
    if model:
        config["llm"]["model"] = model
    if url:
        config["llm"]["url"] = url

    display = Display()
    client = LLMClient(config)
    tools = ToolRegistry(config)

    # Check connection
    ok, err = client.check_connection()
    if not ok:
        display.show_error(err, hint="Is the LLM server running?")
        sys.exit(1)

    # Load context
    context_text, context_count = _load_context(config)
    system_prompt = _build_system_prompt(config, context_text)

    # Join question arguments into a single string
    question_text = " ".join(question) if question else None

    # Detect pipe mode
    piped_input = None
    if not sys.stdin.isatty():
        piped_input = sys.stdin.read()

    # --- Pipe mode ---
    if piped_input:
        if not question_text:
            question_text = "Analyze the following input and describe what you see."

        user_input = (
            f"The user has piped the following input:\n"
            f"```\n{piped_input.strip()}\n```\n\n"
            f"{question_text}"
        )
        messages = []
        _run_turn(client, tools, display, messages, system_prompt, user_input)
        _save_and_cleanup(config, messages)
        client.close()
        return

    # --- One-shot mode ---
    if question_text:
        display.show_welcome(
            config["llm"]["provider"], config["llm"]["model"], context_count
        )
        messages = []
        _run_turn(client, tools, display, messages, system_prompt, question_text)
        _save_and_cleanup(config, messages)
        client.close()
        return

    # --- REPL mode ---
    display.show_welcome(
        config["llm"]["provider"], config["llm"]["model"], context_count
    )

    history_file = DEFAULT_CONFIG_DIR / "history"
    session = PromptSession(
        history=FileHistory(str(history_file)),
    )

    messages = []

    try:
        while True:
            try:
                user_input = session.prompt(
                    HTML("<ansigreen>> </ansigreen>"),
                ).strip()
            except KeyboardInterrupt:
                continue  # Ctrl-C at prompt clears input
            except EOFError:
                break  # Ctrl-D exits

            if not user_input:
                continue

            # Special commands
            if user_input.lower() in ("/exit", "/quit", "exit", "quit"):
                break
            if user_input.lower() in ("/clear", "clear"):
                messages.clear()
                display.show_info("Conversation cleared.")
                continue
            if user_input.lower() in ("/help", "help"):
                display.show_info("Commands: /clear, /exit, /help")
                display.show_info("Type any question to chat with the LLM.")
                display.show_info("Ctrl-D to exit, Ctrl-C to cancel.")
                continue

            _run_turn(client, tools, display, messages, system_prompt, user_input)

    except KeyboardInterrupt:
        pass
    finally:
        _save_and_cleanup(config, messages)
        client.close()
        display.console.print()  # clean newline on exit
