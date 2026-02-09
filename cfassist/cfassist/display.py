"""Rich-based terminal rendering for cfassist."""

import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text
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
})


class Display:
    def __init__(self):
        self.console = Console(theme=THEME)
        self._streaming = False

    def show_welcome(self, provider, model, context_count=0):
        parts = [
            f"[banner]cfassist[/banner] [banner.dim]v{__version__}[/banner.dim]",
            f"[info]{provider}/{model}[/info]",
        ]
        if context_count > 0:
            parts.append(f"[info]{context_count} context file{'s' if context_count != 1 else ''} loaded[/info]")
        self.console.print(" | ".join(parts))
        self.console.print()

    def stream_token(self, token):
        """Print a single token inline during streaming."""
        if not self._streaming:
            self._streaming = True
        self.console.print(token, end="", highlight=False)

    def end_stream(self):
        """Finalize streamed response."""
        if self._streaming:
            self.console.print()  # newline
            self.console.print()  # blank line separator
            self._streaming = False

    def show_response(self, text):
        """Render a complete response (non-streamed)."""
        md = Markdown(text)
        self.console.print(md)
        self.console.print()

    def show_tool_call(self, name, args):
        """Display a tool invocation."""
        if self._streaming:
            self.console.print()  # newline if mid-stream
            self._streaming = False

        if name == "bash":
            cmd = args.get("command", "")
            self.console.print(f"[tool.name]\\[tool] bash:[/tool.name] {cmd}")
        elif name == "read_file":
            path = args.get("path", "")
            self.console.print(f"[tool.name]\\[tool] read_file:[/tool.name] {path}")
        else:
            self.console.print(f"[tool.name]\\[tool] {name}:[/tool.name] {args}")

    def show_tool_result(self, name, result):
        """Display a tool result summary."""
        if isinstance(result, dict):
            if "error" in result:
                self.console.print(f"[tool.error]\\[tool] error: {result['error']}[/tool.error]")
                return

            if name == "bash":
                stdout = result.get("stdout", "")
                stderr = result.get("stderr", "")
                exit_code = result.get("exit_code", 0)
                lines = len(stdout.splitlines()) if stdout else 0
                style = "tool.success" if exit_code == 0 else "tool.error"
                self.console.print(f"[{style}]\\[tool] {lines} lines | exit {exit_code}[/{style}]")
                return

            if name == "read_file":
                content = result.get("content", "")
                lines = len(content.splitlines()) if content else 0
                self.console.print(f"[tool.success]\\[tool] {lines} lines[/tool.success]")
                return

        self.console.print(f"[tool.result]\\[tool] done[/tool.result]")

    def show_thinking(self):
        """Show a thinking indicator."""
        self._thinking = True
        sys.stderr.write("\033[2mthinking...\033[0m")
        sys.stderr.flush()

    def clear_thinking(self):
        """Clear the thinking indicator."""
        if getattr(self, "_thinking", False):
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()
            self._thinking = False

    def show_error(self, message, hint=None):
        """Display an error message."""
        self.console.print(f"[error]{message}[/error]")
        if hint:
            self.console.print(f"[info]  {hint}[/info]")
        self.console.print()

    def show_warning(self, message):
        """Display a warning message."""
        self.console.print(f"[warning]{message}[/warning]")

    def show_info(self, message):
        """Display an info message."""
        self.console.print(f"[info]{message}[/info]")
