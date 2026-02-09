"""Tool registry and execution — bash and read_file."""

import json
import subprocess
from pathlib import Path


class ToolRegistry:
    """Registry of tools the LLM can call."""

    def __init__(self, config):
        self.config = config.get("tools", {})
        self._tools = {}
        self._register_defaults()

    def _register_defaults(self):
        bash_cfg = self.config.get("bash", {})
        if bash_cfg.get("enabled", True):
            self._tools["bash"] = {
                "function": self._bash,
                "schema": {
                    "name": "bash",
                    "description": (
                        "Execute a shell command and return stdout, stderr, and exit code. "
                        "Use for checking system state, running diagnostics, reading logs, "
                        "querying APIs, network checks, and any system administration task."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "The shell command to execute",
                            },
                        },
                        "required": ["command"],
                    },
                },
                "timeout": bash_cfg.get("timeout", 30),
            }

        read_cfg = self.config.get("read_file", {})
        if read_cfg.get("enabled", True):
            self._tools["read_file"] = {
                "function": self._read_file,
                "schema": {
                    "name": "read_file",
                    "description": (
                        "Read the contents of a file and return it as text. "
                        "Use for reading configuration files, logs, scripts, or any text file."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Absolute or relative path to the file",
                            },
                            "max_lines": {
                                "type": "integer",
                                "description": "Maximum number of lines to read (default 500)",
                            },
                        },
                        "required": ["path"],
                    },
                },
                "max_lines": read_cfg.get("max_lines", 500),
            }

    def get_schemas(self):
        """Return tool schemas in OpenAI function-calling format."""
        return [
            {"type": "function", "function": tool["schema"]}
            for tool in self._tools.values()
        ]

    def execute(self, name, arguments):
        """Execute a tool by name with the given arguments."""
        if name not in self._tools:
            return {"error": f"Unknown tool: {name}"}

        tool = self._tools[name]
        try:
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            return tool["function"](arguments)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    def _bash(self, args):
        """Execute a shell command."""
        command = args.get("command", "")
        if not command:
            return {"error": "No command provided"}

        timeout = self._tools["bash"]["timeout"]
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"error": f"Command timed out after {timeout}s"}

    def _read_file(self, args):
        """Read a file's contents."""
        path_str = args.get("path", "")
        if not path_str:
            return {"error": "No path provided"}

        max_lines = args.get("max_lines", self._tools["read_file"]["max_lines"])
        path = Path(path_str).expanduser()

        if not path.exists():
            return {"error": f"File not found: {path}"}
        if not path.is_file():
            return {"error": f"Not a file: {path}"}

        try:
            lines = path.read_text().splitlines()
            truncated = len(lines) > max_lines
            content = "\n".join(lines[:max_lines])
            result = {"content": content, "lines": len(lines)}
            if truncated:
                result["truncated"] = True
                result["showing"] = max_lines
            return result
        except Exception as e:
            return {"error": f"Failed to read {path}: {e}"}
