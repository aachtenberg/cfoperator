"""Node-action command plan helpers for the agent change-record gate.

Mirrors the executor allowlist / prompt / parse so the plan stamped into the
change record at open() is the same shape the executor will run after approval.
Kept behaviourally identical to ``executor/nodeaction.py`` -- deliberately a
copy, not an import: the executor is a standalone portable image and must not
depend on the monolith. The parity test proves the two agree.

Neither module ORIGINATES the allowlist any more (CFOP-133). The agent resolves
it from config + the console's selection and passes the effective list here and
into the executor Job's environment; both sides then enforce what they were
handed and neither can widen it. Stdlib only.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

# ---- the floor: hardcoded on purpose, not configurable (CFOP-133) -----------
#
# Catastrophic binaries refused even if they are somehow named in the allowlist,
# and the metacharacters that would chain/redirect/expand around the gate. These
# are NOT a capability list -- they are the "never, under any circumstances"
# rules, and their whole value is that no config file, no database row and no
# compromised console can switch them off. Keep identical to the executor's.
_DENY_BINARIES = {
    "rm", "rmdir", "dd", "mkfs", "shutdown", "reboot", "halt", "poweroff",
    "kill", "pkill", "killall", "userdel", "useradd", "usermod", "passwd",
    "iptables", "nft", "wipefs", "fdisk", "parted", "mv", "cp", "sh", "bash",
    "curl", "wget", "eval", "exec", "tee", "sed", "awk", "python", "python3",
}
_METACHARS = re.compile(r"[;&|<>`$(){}\[\]*?~\n\r\\]|\$\(|&&|\|\|")


class AllowList(NamedTuple):
    """What a node-action may run. No default: an empty AllowList refuses all."""

    binaries: frozenset
    systemctl_verbs: frozenset
    max_commands: int

    @property
    def configured(self) -> bool:
        return bool(self.binaries)


def _split_list(raw) -> frozenset:
    """Parse a comma/whitespace separated list (or an iterable) into a set."""
    if raw is None:
        return frozenset()
    if not isinstance(raw, str):
        try:
            return frozenset(str(t).strip() for t in raw if str(t).strip())
        except TypeError:
            return frozenset()
    return frozenset(t for t in re.split(r"[,\s]+", raw.strip()) if t)


def allowlist_from_config(ceiling: Dict[str, Any],
                          selected_binaries=None,
                          selected_verbs=None) -> AllowList:
    """Resolve the effective allowlist: config declares the ceiling, the console picks within it.

    ``ceiling`` is the ``node_action`` config block. The selections come from
    the database (console-written) and may only ever NARROW -- they are
    intersected with the ceiling, never unioned. An unset selection (empty)
    means "the whole ceiling", which is a first-class state and not an empty
    list; that is what lets an operator clear a row back to the default.

    A ceiling that names nothing yields an AllowList that refuses everything.
    That is the intended failure mode: an install that has not declared what
    node-actions may run does not get a built-in guess.
    """
    binaries = _split_list(ceiling.get('allow_binaries'))
    verbs = _split_list(ceiling.get('allow_systemctl_verbs'))
    picked_b = _split_list(selected_binaries)
    picked_v = _split_list(selected_verbs)
    if picked_b:
        binaries &= picked_b
    if picked_v:
        verbs &= picked_v
    try:
        max_commands = int(ceiling.get('max_commands') or 4)
    except (TypeError, ValueError):
        max_commands = 4
    return AllowList(binaries=binaries, systemctl_verbs=verbs,
                     max_commands=max(1, max_commands))


def build_command_prompt(work_order: Dict[str, Any], allow: AllowList) -> str:
    """Ask the LLM to translate the recommendation into a concrete command plan.

    The rules are GENERATED from ``allow``, never written by hand (CFOP-133).
    Both copies of this prompt used to spell the list out, and both had drifted
    from the gate beside them: they named 5 systemctl verbs where 9 were
    accepted, and 8 denied binaries out of 33. A hand-written prompt also
    breaks the operator dial outright once the list is configurable — a binary
    added in the console would be accepted by the gate but never offered to the
    model, so the change would appear to do nothing.
    """
    payload = work_order.get("payload") or {}
    target = payload.get("target") or {}
    binaries = ", ".join(sorted(allow.binaries)) or "(none — every command will be refused)"
    verbs = ", ".join(sorted(allow.systemctl_verbs)) or "(none)"
    return (
        "You are a careful site-reliability operator translating a remediation "
        "recommendation into concrete shell commands to run on ONE host over SSH.\n\n"
        f"Recommendation: {payload.get('recommendation', '')}\n"
        f"Target: {json.dumps(target)}\n"
        f"Context: {str(payload.get('rendered_context', ''))[:4000]}\n\n"
        "Rules:\n"
        f"- Output at most {allow.max_commands} command(s); prefer one or two.\n"
        "- Each command must be a single, simple command (NO pipes, &&, ;, "
        "redirection, globbing, command substitution, or shell builtins).\n"
        f"- Allowed binaries: {binaries}.\n"
        f"- systemctl is allowed only with these verbs: {verbs}.\n"
        "- Prefix with 'sudo -n' if (and only if) root is required.\n"
        "- Anything not named above is refused, as is any data-destructive or "
        "network command.\n\n"
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


def validate_command(command: str, allow: AllowList) -> Tuple[bool, str]:
    raw = (command or "").strip()
    if not raw:
        return False, "empty command"
    if not allow.configured:
        return False, ("no allowlist configured for this Job — refusing every "
                       "command (set remediation.executor.node_action.allow_binaries)")
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
    if binary not in allow.binaries:
        return False, f"binary not in allowlist: {binary}"
    if binary == "systemctl":
        verb = tokens[1] if len(tokens) > 1 else ""
        if verb not in allow.systemctl_verbs:
            return False, f"systemctl verb not allowed: {verb!r}"
    return True, "ok"


def validate_plan(commands: List[str], allow: AllowList) -> Tuple[bool, str]:
    if not commands:
        return False, "plan has no commands"
    if len(commands) > allow.max_commands:
        return False, f"plan has too many commands ({len(commands)} > {allow.max_commands})"
    for cmd in commands:
        ok, reason = validate_command(cmd, allow)
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
