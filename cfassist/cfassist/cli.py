"""CLI entry point — REPL (TUI), one-shot, and pipe modes."""

import sys
import threading

import click
from prompt_toolkit import Application
from prompt_toolkit.cursor_shapes import CursorShape
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    Layout,
    HSplit,
    Window,
    FormattedTextControl,
)
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.styles import Style as PTStyle
from prompt_toolkit.widgets import TextArea

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


def _run_tui(config, client, tools, system_prompt, context_count):
    """Run the full-screen TUI REPL."""
    messages = []
    history_file = DEFAULT_CONFIG_DIR / "history"

    # --- Output state: accumulate ANSI text, render via FormattedTextControl ---
    output_lines = []  # list of ANSI strings

    def append_output(text):
        """Append text to the output pane and auto-scroll."""
        output_lines.append(text)
        try:
            app.invalidate()
        except Exception:
            pass

    def get_output_text():
        """Return accumulated output as ANSI formatted text."""
        return ANSI("".join(output_lines))

    # --- Display wired to output buffer ---
    display = Display(output_callback=append_output)

    # Show welcome banner
    display.show_welcome(
        config["llm"]["provider"], config["llm"]["model"], context_count
    )

    # --- Input area (bottom pane) ---
    input_area = TextArea(
        height=D.exact(3),
        prompt=" > ",
        multiline=False,
        history=FileHistory(str(history_file)),
        style="class:input-area",
    )

    # Flag to prevent overlapping LLM calls
    _busy = threading.Event()

    def handle_accept(buff):
        """Called when user presses Enter in the input area."""
        text = buff.text.strip()
        if not text:
            return

        # Special commands
        cmd = text.lower()
        if cmd in ("/exit", "/quit", "exit", "quit"):
            app.exit()
            return
        if cmd in ("/clear", "clear"):
            messages.clear()
            output_lines.clear()
            display.show_welcome(
                config["llm"]["provider"], config["llm"]["model"], context_count
            )
            return
        if cmd in ("/help", "help"):
            display.show_info("Commands: /clear, /exit, /help")
            display.show_info("Ctrl-D to exit, Ctrl-C to cancel input.")
            return

        if _busy.is_set():
            return

        # Show the user's message in output
        append_output(f"\n\033[1;32m>\033[0m {text}\n\n")

        # Run LLM in background thread so UI stays responsive
        def worker():
            _busy.set()
            try:
                _run_turn(client, tools, display, messages, system_prompt, text)
            except Exception as e:
                display.show_error(f"Error: {e}")
            finally:
                _busy.clear()

        t = threading.Thread(target=worker, daemon=True)
        t.start()

    input_area.buffer.accept_handler = handle_accept

    # --- Key bindings ---
    kb = KeyBindings()

    @kb.add("c-d")
    def exit_app(event):
        event.app.exit()

    @kb.add("c-c")
    def cancel_input(event):
        input_area.buffer.reset()

    # --- Status bar ---
    def get_status_text():
        model = config["llm"]["model"]
        status = "working..." if _busy.is_set() else "ready"
        return [("class:status", f" cfassist v{__version__} | {model} | {status} ")]

    status_bar = Window(
        content=FormattedTextControl(get_status_text),
        height=D.exact(1),
        style="class:status",
    )

    # --- Separator ---
    separator = Window(
        height=D.exact(1),
        char="─",
        style="class:separator",
    )

    # --- Layout: output pane uses FormattedTextControl with ANSI parsing ---
    output_control = FormattedTextControl(
        text=get_output_text,
        focusable=False,
        show_cursor=False,
    )

    output_window = Window(
        content=output_control,
        wrap_lines=True,
    )

    root = HSplit([
        output_window,     # scrollable conversation — takes all remaining space
        separator,         # thin line
        status_bar,        # model + status
        input_area,        # fixed input at bottom
    ])

    # --- Style ---
    style = PTStyle.from_dict({
        "input-area":  "bg:#0d0d1a #cccccc",     # transparent black input bg
        "status":      "bg:#1a1a2e #888888",      # dim status bar
        "separator":   "#333333",                  # subtle separator
    })

    # --- Application ---
    app = Application(
        layout=Layout(root, focused_element=input_area),
        style=style,
        key_bindings=kb,
        full_screen=True,
        cursor=CursorShape.BLINKING_BEAM,
        refresh_interval=0.5,  # auto-refresh for status bar updates
    )

    app.run()

    # Cleanup after TUI exits
    _save_and_cleanup(config, messages)
    client.close()


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

    # Load context
    context_text, context_count = _load_context(config)
    system_prompt = _build_system_prompt(config, context_text)

    # Join question arguments into a single string
    question_text = " ".join(question) if question else None

    # Detect pipe mode
    piped_input = None
    if not sys.stdin.isatty():
        piped_input = sys.stdin.read()

    # --- Pipe mode (non-interactive) ---
    if piped_input:
        display = Display()
        client = LLMClient(config)
        tools = ToolRegistry(config)

        ok, err = client.check_connection()
        if not ok:
            display.show_error(err, hint="Is the LLM server running?")
            sys.exit(1)

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

    # --- One-shot mode (non-interactive) ---
    if question_text:
        display = Display()
        client = LLMClient(config)
        tools = ToolRegistry(config)

        ok, err = client.check_connection()
        if not ok:
            display.show_error(err, hint="Is the LLM server running?")
            sys.exit(1)

        display.show_welcome(
            config["llm"]["provider"], config["llm"]["model"], context_count
        )
        messages = []
        _run_turn(client, tools, display, messages, system_prompt, question_text)
        _save_and_cleanup(config, messages)
        client.close()
        return

    # --- TUI REPL mode ---
    client = LLMClient(config)
    tools = ToolRegistry(config)

    ok, err = client.check_connection()
    if not ok:
        display = Display()
        display.show_error(err, hint="Is the LLM server running?")
        sys.exit(1)

    _run_tui(config, client, tools, system_prompt, context_count)
