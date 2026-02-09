"""Display layer — writes to a buffer for TUI mode or to console for non-interactive."""

import io
import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.theme import Theme

from cfassist import __version__

THEME = Theme({
    "tool.name": "dim cyan",
    "tool.result": "dim",
    "tool.success": "dim green",
    "tool.error": "dim red",
    "info": "dim",
    "warning": "yellow",
    "error": "bold red",
    "banner": "bold green",
    "banner.dim": "dim green",
    "separator": "dim",
})


class Display:
    """Renders output to a callback function or to stdout.

    In TUI mode, set `output_callback` to a function that accepts a string.
    All output goes through that callback so the TUI can append it to the
    scrollable output pane.

    In non-interactive mode (pipe/one-shot), output goes to stdout via Rich.
    """

    def __init__(self, output_callback=None):
        self._callback = output_callback
        # Console for non-TUI modes (pipe, one-shot)
        self.console = Console(theme=THEME)
        # Console that renders to string (for TUI mode)
        self._str_console = Console(theme=THEME, file=io.StringIO(), force_terminal=True, width=120)

    def _render(self, *args, **kwargs):
        """Render Rich objects to plain ANSI string."""
        buf = io.StringIO()
        c = Console(theme=THEME, file=buf, force_terminal=True, width=120)
        c.print(*args, **kwargs)
        return buf.getvalue()

    def _emit(self, text):
        """Send text to TUI buffer or stdout."""
        if self._callback:
            self._callback(text)
        else:
            self.console.print(text, highlight=False)

    def _emit_rich(self, *args, **kwargs):
        """Render Rich markup and emit."""
        rendered = self._render(*args, **kwargs)
        if self._callback:
            self._callback(rendered)
        else:
            self.console.print(*args, **kwargs)

    def show_welcome(self, provider, model, context_count=0):
        width = 80
        sep = "─" * width
        parts = [
            f"[banner]cfassist[/banner] [banner.dim]v{__version__}[/banner.dim]",
            f"[info]{provider}/{model}[/info]",
        ]
        if context_count > 0:
            parts.append(f"[info]{context_count} context file{'s' if context_count != 1 else ''} loaded[/info]")
        self._emit_rich(f"[separator]{sep}[/separator]")
        self._emit_rich("  " + " | ".join(parts))
        self._emit_rich(f"[separator]{sep}[/separator]")
        self._emit_rich()

    def show_response(self, text):
        """Render a complete response as markdown."""
        rendered = self._render(Markdown(text))
        if self._callback:
            self._callback(rendered + "\n")
        else:
            self.console.print(Markdown(text))
            self.console.print()

    def show_tool_call(self, name, args):
        """Display a tool invocation."""
        if name == "bash":
            cmd = args.get("command", "")
            self._emit_rich(f"[tool.name]\\[tool] bash:[/tool.name] {cmd}")
        elif name == "read_file":
            path = args.get("path", "")
            self._emit_rich(f"[tool.name]\\[tool] read_file:[/tool.name] {path}")
        else:
            self._emit_rich(f"[tool.name]\\[tool] {name}:[/tool.name] {args}")

    def show_tool_result(self, name, result):
        """Display a tool result summary."""
        if isinstance(result, dict):
            if "error" in result:
                self._emit_rich(f"[tool.error]\\[tool] error: {result['error']}[/tool.error]")
                return

            if name == "bash":
                stdout = result.get("stdout", "")
                exit_code = result.get("exit_code", 0)
                lines = len(stdout.splitlines()) if stdout else 0
                style = "tool.success" if exit_code == 0 else "tool.error"
                self._emit_rich(f"[{style}]\\[tool] {lines} lines | exit {exit_code}[/{style}]")
                return

            if name == "read_file":
                content = result.get("content", "")
                lines = len(content.splitlines()) if content else 0
                self._emit_rich(f"[tool.success]\\[tool] {lines} lines[/tool.success]")
                return

        self._emit_rich(f"[tool.result]\\[tool] done[/tool.result]")

    def show_thinking(self):
        """Show thinking indicator."""
        if self._callback:
            # TUI mode — append to output, will scroll naturally
            self._emit_rich("[dim]  thinking...[/dim]")
        else:
            # Non-TUI — overwritable inline indicator
            sys.stdout.write("\033[2m  thinking...\033[0m")
            sys.stdout.flush()
            self._thinking = True

    def clear_thinking(self):
        """Clear thinking indicator."""
        if not self._callback and getattr(self, "_thinking", False):
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            self._thinking = False

    def show_error(self, message, hint=None):
        """Display an error message."""
        self._emit_rich(f"[error]{message}[/error]")
        if hint:
            self._emit_rich(f"[info]  {hint}[/info]")

    def show_warning(self, message):
        """Display a warning message."""
        self._emit_rich(f"[warning]{message}[/warning]")

    def show_info(self, message):
        """Display an info message."""
        self._emit_rich(f"[info]{message}[/info]")
