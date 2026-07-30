"""Node-action command plan helpers for the agent change-record gate.

Mirrors the executor allowlist / prompt / parse so the plan stamped into the
change record at open() is the same shape the executor will run after approval.
Keep in sync with ``executor/nodeaction.py``. Stdlib only.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any, Dict, List, Optional, Tuple

# Keep these sets identical to executor/nodeaction.py.
_ALLOWED_BINARIES = {
    "chmod", "chown", "chgrp", "ln", "mkdir", "install", "touch", "restorecon",
    "chattr", "systemctl",
}
_ALLOWED_SYSTEMCTL_VERBS = {
    "restart", "start", "reload", "reload-or-restart", "status", "is-active",
    "is-enabled", "enable", "daemon-reload",
}
_DENY_BINARIES = {
    "rm", "rmdir", "dd", "mkfs", "shutdown", "reboot", "halt", "poweroff",
    "kill", "pkill", "killall", "userdel", "useradd", "usermod", "passwd",
    "iptables", "nft", "wipefs", "fdisk", "parted", "mv", "cp", "sh", "bash",
    "curl", "wget", "eval", "exec", "tee", "sed", "awk", "python", "python3",
}
_METACHARS = re.compile(r"[;&|<>`$(){}\[\]*?~\n\r\\]|\$\(|&&|\|\|")
_MAX_COMMANDS = 4


def build_command_prompt(work_order: Dict[str, Any]) -> str:
    """Ask the LLM to translate the recommendation into a concrete command plan."""
    payload = work_order.get("payload") or {}
    target = payload.get("target") or {}
    return (
        "You are a careful site-reliability operator translating a remediation "
        "recommendation into concrete shell commands to run on ONE host over SSH.\n\n"
        f"Recommendation: {payload.get('recommendation', '')}\n"
        f"Target: {json.dumps(target)}\n"
        f"Context: {str(payload.get('rendered_context', ''))[:4000]}\n\n"
        "Rules:\n"
        "- Output ONLY the minimal commands needed; prefer one or two.\n"
        "- Each command must be a single, simple command (NO pipes, &&, ;, "
        "redirection, globbing, command substitution, or shell builtins).\n"
        "- Allowed binaries: chmod, chown, chgrp, ln, mkdir, install, touch, "
        "restorecon, chattr, systemctl (restart/start/reload/status/enable only).\n"
        "- Prefix with 'sudo -n' if (and only if) root is required.\n"
        "- NEVER use rm, dd, mv, cp, reboot, kill, curl, sed, or any data-destructive "
        "or network command.\n\n"
        "Reply with EXACTLY one JSON object and nothing else:\n"
        '{"host": "<hostname or empty to use the configured default>", '
        '"commands": ["cmd1", "cmd2"], "explanation": "one line"}'
    )


def parse_command_plan(reply: str) -> Optional[Dict[str, Any]]:
    """Extract the JSON command plan from the LLM reply (fenced or bare)."""
    text = reply or ""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    blob = fenced.group(1) if fenced else None
    if blob is None:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None
        blob = text[start:end + 1]
    try:
        plan = json.loads(blob)
    except ValueError:
        return None
    if not isinstance(plan, dict):
        return None
    cmds = plan.get("commands")
    if not isinstance(cmds, list) or not all(isinstance(c, str) for c in cmds):
        return None
    return plan


def validate_command(command: str) -> Tuple[bool, str]:
    raw = (command or "").strip()
    if not raw:
        return False, "empty command"
    if _METACHARS.search(raw):
        return False, f"shell metacharacter in command: {raw!r}"
    try:
        tokens = shlex.split(raw)
    except ValueError as e:
        return False, f"unparseable command ({e}): {raw!r}"
    if not tokens:
        return False, "no command tokens"
    if tokens[0] == "sudo":
        if len(tokens) < 3 or tokens[1] != "-n":
            return False, "sudo is only allowed as 'sudo -n <command>'"
        tokens = tokens[2:]
    binary = tokens[0]
    if binary in _DENY_BINARIES:
        return False, f"binary is explicitly denied: {binary}"
    if binary not in _ALLOWED_BINARIES:
        return False, f"binary not in allowlist: {binary}"
    if binary == "systemctl":
        verb = tokens[1] if len(tokens) > 1 else ""
        if verb not in _ALLOWED_SYSTEMCTL_VERBS:
            return False, f"systemctl verb not allowed: {verb!r}"
    return True, "ok"


def validate_plan(commands: List[str]) -> Tuple[bool, str]:
    if not commands:
        return False, "plan has no commands"
    if len(commands) > _MAX_COMMANDS:
        return False, f"plan has too many commands ({len(commands)} > {_MAX_COMMANDS})"
    for cmd in commands:
        ok, reason = validate_command(cmd)
        if not ok:
            return False, reason
    return True, "ok"


def normalize_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Return a stable {host, commands, explanation} dict for the record / work order."""
    return {
        "host": str(plan.get("host") or "").strip(),
        "commands": [str(c) for c in (plan.get("commands") or [])],
        "explanation": str(plan.get("explanation") or ""),
    }
