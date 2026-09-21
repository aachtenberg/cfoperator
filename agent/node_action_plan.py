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


# Console / DB keys for the operator's subset of the ceiling (CFOP-132).
# Unset ('') means the whole ceiling; a stored list may only ever narrow.
SETTING_BINARIES = "node_action_allow_binaries"
SETTING_VERBS = "node_action_allow_systemctl_verbs"


class AllowlistEditError(ValueError):
    """Caller-correctable allowlist POST: names off the ceiling, empty pick, etc."""


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
    intersected with the ceiling, never unioned. An unset selection (``''``)
    means "the whole ceiling", which is a first-class state and not an empty
    list; that is what lets an operator clear a row back to the default.

    A selection of ``None`` means the read FAILED, and refuses everything.
    Deliberately distinct from unset: collapsing the two would make a database
    blip silently restore binaries an operator had removed, which is a silent
    undo of the only control this mechanism adds. Unset is the one path back
    to the ceiling.

    A ceiling that names nothing yields an AllowList that refuses everything.
    That is the intended failure mode: an install that has not declared what
    node-actions may run does not get a built-in guess.
    """
    if selected_binaries is None or selected_verbs is None:
        return AllowList(frozenset(), frozenset(), 1)
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


def _name_list(raw) -> List[str]:
    """Coerce a POST field (list or comma-string) into stripped unique names."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return sorted(_split_list(raw))
    if isinstance(raw, (list, tuple, set)):
        names = []
        for item in raw:
            token = str(item).strip()
            if token:
                names.append(token)
        return sorted(set(names))
    raise AllowlistEditError("binaries and verbs must be lists of names")


def stored_selection(ceiling: Dict[str, Any], binaries, verbs) -> Tuple[str, str]:
    """Turn a console POST into the two DB strings ``_node_action_setting`` reads.

    Names not on the ceiling raise ``AllowlistEditError`` rather than being
    silently dropped: dropping would make ``journalctl`` look saved when the
    next Job still refuses it. An empty pick is also an error — refusing every
    node-action is the existing kill-switch, and restoring the ceiling is an
    explicit reset (store ``''``). A pick that equals the ceiling also stores
    ``''``, so a later deploy that adds a ceiling member is picked up instead
    of being frozen out by a stale full-list row.
    """
    ceil_b = _split_list(ceiling.get("allow_binaries"))
    ceil_v = _split_list(ceiling.get("allow_systemctl_verbs"))
    picked_b = set(_name_list(binaries))
    picked_v = set(_name_list(verbs))
    extra_b = sorted(picked_b - ceil_b)
    extra_v = sorted(picked_v - ceil_v)
    if extra_b or extra_v:
        extra = extra_b + extra_v
        raise AllowlistEditError(
            "not on the deployed ceiling (needs a config commit): " + ", ".join(extra)
        )
    if not picked_b:
        raise AllowlistEditError(
            "select at least one binary; to refuse every node-action use the "
            "kill-switch, to restore the ceiling use Reset"
        )
    if not picked_v:
        raise AllowlistEditError(
            "select at least one systemctl verb; to restore the ceiling use Reset"
        )
    stored_b = "" if picked_b == ceil_b else ",".join(sorted(picked_b))
    stored_v = "" if picked_v == ceil_v else ",".join(sorted(picked_v))
    return stored_b, stored_v


def allowlist_view(ceiling: Dict[str, Any],
                   selected_binaries: Optional[str],
                   selected_verbs: Optional[str]) -> Dict[str, Any]:
    """Console GET payload: ceiling, selection, effective set, and the floor.

    ``selected_*`` of ``None`` means the DB read failed — same refuse-all
    posture as ``allowlist_from_config``. ``''`` is unset (whole ceiling).
    The floor is included so an operator who cannot see it cannot reason
    about the gate; it is never writable from this payload.
    """
    ceil_b = sorted(_split_list(ceiling.get("allow_binaries")))
    ceil_v = sorted(_split_list(ceiling.get("allow_systemctl_verbs")))
    try:
        max_commands = max(1, int(ceiling.get("max_commands") or 4))
    except (TypeError, ValueError):
        max_commands = 4
    floor = {
        "deny_binaries": sorted(_DENY_BINARIES),
        "metacharacters": _METACHARS.pattern,
    }
    if selected_binaries is None or selected_verbs is None:
        return {
            "ceiling": {"binaries": ceil_b, "verbs": ceil_v,
                        "max_commands": max_commands},
            "selected": {"binaries": None, "verbs": None},
            "effective": {"binaries": [], "verbs": [], "max_commands": 1},
            "source": "error",
            "floor": floor,
        }
    allow = allowlist_from_config(ceiling, selected_binaries, selected_verbs)
    sel_b = None if selected_binaries == "" else sorted(_split_list(selected_binaries))
    sel_v = None if selected_verbs == "" else sorted(_split_list(selected_verbs))
    source = "config" if sel_b is None and sel_v is None else "db"
    return {
        "ceiling": {"binaries": ceil_b, "verbs": ceil_v,
                    "max_commands": allow.max_commands},
        "selected": {"binaries": sel_b, "verbs": sel_v},
        "effective": {
            "binaries": sorted(allow.binaries),
            "verbs": sorted(allow.systemctl_verbs),
            "max_commands": allow.max_commands,
        },
        "source": source,
        "floor": floor,
    }


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
