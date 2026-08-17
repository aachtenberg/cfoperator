"""CLI entry point — REPL (TUI), one-shot, pipe, and `attach` modes."""

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
from cfassist.briefing import (
    ATTACH_GUIDANCE,
    ATTACH_VERB,
    build_briefing,
    parse_investigation_ref,
)
from cfassist.cfoperator import (
    CFOperatorClient,
    CFOperatorError,
    resolve_endpoint,
)
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


def _run_tui(config, client, tools, system_prompt, context_count, preamble=None):
    """Run the full-screen TUI REPL.

    ``preamble`` is printed into the output pane before the first prompt — used
    by `attach` so the operator reads the same briefing the model was seeded
    with, rather than having to ask it what it knows.
    """
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
    if preamble:
        display.show_briefing(preamble)

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
            # /clear drops the transcript, not the attachment: the briefing is
            # in the system prompt and the model is still attached, so redraw it
            # rather than leave the operator looking at an empty session.
            if preamble:
                display.show_briefing(preamble)
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


def _shared_options(fn):
    """Options accepted at both the group and the subcommand level.

    Both levels, because `cfassist --model m "question"` was valid when this
    was one flat command (click accepts options in any position there) and must
    stay valid now that it is a group. `_merged` resolves the two.
    """
    for option in reversed([
        click.option("--config", "config_path", default=None,
                     help="Path to config file"),
        click.option("--model", default=None, help="Override LLM model"),
        click.option("--url", default=None, help="Override LLM endpoint URL"),
    ]):
        fn = option(fn)
    return fn


def _merged(ctx, config_path, model, url):
    """Subcommand options win; the group's are the fallback."""
    shared = (ctx.obj if ctx is not None else None) or {}
    return (config_path or shared.get("config_path"),
            model or shared.get("model"),
            url or shared.get("url"))


def _prepare(config_path, model, url):
    """Load config, apply CLI overrides, build the system prompt."""
    config = load_config(config_path)
    ensure_directories(config)

    if model:
        config["llm"]["model"] = model
    if url:
        config["llm"]["url"] = url

    context_text, context_count = _load_context(config)
    return config, _build_system_prompt(config, context_text), context_count


class _DefaultCommandGroup(click.Group):
    """A Group whose unrecognised first argument is treated as a question.

    cfassist has always been invoked as ``cfassist <free-form question>``.
    CFOP-29 adds a real verb (``attach``), and a plain Group would turn every
    existing invocation into "No such command 'why'". Unknown first arguments
    therefore fall through to ``chat``, which carries the original behaviour.

    The alternative considered — sniffing for the literal word "attach" at the
    head of the question — was rejected: it cannot tell ``cfassist attach 1889``
    from ``cfassist "attach the log to the ticket"`` without inspecting whether
    the *next* token happens to be an integer, and a verb whose recognition
    depends on its argument's type is not a verb.
    """

    DEFAULT_COMMAND = "chat"

    def resolve_command(self, ctx, args):
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError:
            default = self.commands.get(self.DEFAULT_COMMAND)
            if default is None:
                raise
            # Hand the arguments over untouched; `chat` takes them as nargs=-1.
            return default.name, default, args


@click.group(cls=_DefaultCommandGroup, invoke_without_command=True)
@_shared_options
@click.option("--version", is_flag=True, help="Show version")
@click.pass_context
def main(ctx, config_path, model, url, version):
    """cfassist — CLI assistant for SRE and systems administration.

    Run without arguments for interactive mode.
    Pass a question for one-shot mode.
    Pipe data in for analysis mode.
    Run `cfassist attach <investigation-id>` to open a session briefed with a
    CFOperator investigation.
    """
    ctx.obj = {"config_path": config_path, "model": model, "url": url}
    if version:
        click.echo(f"cfassist {__version__}")
        ctx.exit()
    if ctx.invoked_subcommand is None:
        ctx.invoke(chat, question=())


@main.command()
@click.argument("question", nargs=-1)
@_shared_options
@click.pass_context
def chat(ctx, question, config_path, model, url):
    """Ask a question, or drop into the interactive REPL."""
    config, system_prompt, context_count = _prepare(*_merged(ctx, config_path, model, url))

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


def fetch_briefing(config, investigation_ref):
    """Pull an investigation and render its briefing. Read-only throughout.

    Split out from the command body so the network shape and the rendering can
    be exercised without click, and so a caller that only wants the text (the
    ``--print`` path, a future MCP/pod tier) does not drag in the LLM client.
    """
    investigation_id = parse_investigation_ref(investigation_ref)
    url, token, timeout = resolve_endpoint(config.get("cfoperator"))
    if not token:
        raise CFOperatorError(
            "No CFOperator API token configured",
            hint="export CFOP_API_TOKEN=… (mint one at "
                 f"{url}/admin?tab=tokens) or set cfoperator.token in "
                 "~/.cfassist/config.yaml",
        )
    with CFOperatorClient(url=url, token=token, timeout=timeout) as cf:
        context = cf.collect_attach_context(investigation_id)
    return build_briefing(context)


@main.command(name=ATTACH_VERB)
@click.argument("investigation", metavar="<investigation-id>")
@click.argument("question", nargs=-1)
@_shared_options
@click.option("--print", "print_only", is_flag=True,
              help="Print the briefing and exit; start no session")
@click.pass_context
def attach(ctx, investigation, question, config_path, model, url, print_only):
    """Open a session briefed with CFOperator investigation <investigation-id>.

    Pulls the investigation, its operator triage, any linked remediation queue
    rows and related knowledge-base learnings, seeds them as session context,
    and drops into the REPL. Everything it does against CFOperator is a read.
    """
    display = Display()

    # Validate the reference before any config or directory work: a typo should
    # cost a one-line error, not a config write and a network round trip.
    try:
        parse_investigation_ref(investigation)
    except ValueError as exc:
        display.show_error(str(exc), hint="Usage: cfassist attach <investigation-id>")
        sys.exit(2)

    config, system_prompt, context_count = _prepare(*_merged(ctx, config_path, model, url))

    try:
        briefing = fetch_briefing(config, investigation)
    except CFOperatorError as exc:
        display.show_error(exc.message, hint=exc.hint)
        sys.exit(1)

    # --print is the neutral-contract escape hatch: the briefing is CFOperator's
    # product, and an operator who drives a different agent should be able to
    # pipe it there rather than being forced through this REPL.
    if print_only:
        click.echo(briefing)
        return

    system_prompt = f"{system_prompt}\n\n{ATTACH_GUIDANCE}\n{briefing}"

    llm = LLMClient(config)
    tools = ToolRegistry(config)

    ok, err = llm.check_connection()
    if not ok:
        display.show_error(err, hint="Is the LLM server running?")
        sys.exit(1)

    question_text = " ".join(question).strip() if question else ""
    if question_text:
        display.show_welcome(
            config["llm"]["provider"], config["llm"]["model"], context_count
        )
        display.show_briefing(briefing)
        messages = []
        _run_turn(llm, tools, display, messages, system_prompt, question_text)
        _save_and_cleanup(config, messages)
        llm.close()
        return

    _run_tui(config, llm, tools, system_prompt, context_count, preamble=briefing)
