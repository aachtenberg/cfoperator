"""Node-action execution for the portable remediation executor.

A ``node-action`` is a host change over SSH (file perms, ownership, a systemd
restart) — the kind the GitOps file->PR path cannot express. This module turns
the recommendation into a concrete command plan via the swappable LLM, runs it
through a *deterministic* safety gate, and executes it over SSH.

Safety model:
  * Node-actions are never auto-eligible (see knowledge_base), so they reach the
    executor only after a human approves (escalates) the queue row. That human
    approval is the gate that GitOps gets from a PR merge.
  * Execution is opt-in per deploy (CFOP_NODE_ACTION_ENABLED); shipping the image
    does not silently start running shell on hosts.
  * Every proposed command is validated against an allowlist of non-destructive
    admin binaries with no shell metacharacters — the LLM cannot widen this.

Stdlib only (shlex / subprocess) to keep the executor image minimal & portable.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

logger = logging.getLogger("cfop-executor.nodeaction")

# ---- the floor: hardcoded on purpose, not configurable (CFOP-133) -----------
#
# Catastrophic binaries refused even if they are somehow named in the allowlist,
# and the metacharacters that would chain/redirect/expand around the gate. These
# are NOT a capability list — they are the "never, under any circumstances"
# rules, and their whole value is that no config file, no database row and no
# compromised console can switch them off. Do not make these configurable; the
# allowlist above them is the operator's dial.
_DENY_BINARIES = {
    "rm", "rmdir", "dd", "mkfs", "shutdown", "reboot", "halt", "poweroff",
    "kill", "pkill", "killall", "userdel", "useradd", "usermod", "passwd",
    "iptables", "nft", "wipefs", "fdisk", "parted", "mv", "cp", "sh", "bash",
    "curl", "wget", "eval", "exec", "tee", "sed", "awk", "python", "python3",
}
_METACHARS = re.compile(r"[;&|<>`$(){}\[\]*?~\n\r\\]|\$\(|&&|\|\|")


class AllowList(NamedTuple):
    """What this Job may run, as handed to it by the agent.

    There is deliberately no default and no built-in fallback: the executor
    does not decide what is allowed, it is *told*, and an empty AllowList
    refuses everything. A built-in default here would be the CFOP-131
    pass-through shape again — an unconfigured deploy silently permitting
    work instead of refusing it — and would also mean an executor image newer
    than its agent quietly ran on its own idea of the rules.
    """

    binaries: frozenset
    systemctl_verbs: frozenset
    max_commands: int

    @property
    def configured(self) -> bool:
        return bool(self.binaries)


def _split_list(raw: str) -> frozenset:
    """Parse a comma/whitespace separated env list into a set of tokens."""
    return frozenset(t for t in re.split(r"[,\s]+", (raw or "").strip()) if t)


def allowlist_from_env(env: Dict[str, str]) -> AllowList:
    """The allowlist the agent passed in this Job's environment.

    Absent or empty means refuse everything — never a built-in default. See
    AllowList. ``max_commands`` has a numeric fallback because a bad integer is
    a malformed value rather than a missing permission, and 0 would refuse
    every plan for a reason that reads like a bug.
    """
    try:
        max_commands = int(env.get("CFOP_NODE_ACTION_MAX_COMMANDS") or 4)
    except (TypeError, ValueError):
        max_commands = 4
    return AllowList(
        binaries=_split_list(env.get("CFOP_NODE_ACTION_ALLOW_BINARIES", "")),
        systemctl_verbs=_split_list(env.get("CFOP_NODE_ACTION_ALLOW_SYSTEMCTL_VERBS", "")),
        max_commands=max(1, max_commands),
    )


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
    """Deterministic safety gate for one command. Returns (ok, reason).

    The LLM proposes; this function disposes. It is the security boundary, so it
    is intentionally strict: allowlisted binary, no metacharacters, no globs.

    ``allow`` is supplied by the caller and has no default (CFOP-133): the
    executor enforces the list it was handed and cannot widen it, and an
    unconfigured AllowList refuses everything rather than falling back.
    """
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

    # Optional non-interactive sudo wrapper: 'sudo -n <real command>'.
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
    """Validate the whole command plan; refuse the lot if any command fails."""
    if not commands:
        return False, "plan has no commands"
    if len(commands) > allow.max_commands:
        return False, f"plan has too many commands ({len(commands)} > {allow.max_commands})"
    for cmd in commands:
        ok, reason = validate_command(cmd, allow)
        if not ok:
            return False, reason
    return True, "ok"


def prepare_ssh(secret_dir: Path, ssh_dir: Path) -> None:
    """Copy the mounted SSH secret into ~/.ssh with key-safe permissions.

    Mirrors the forensics worker: secret volumes are root-owned and (at best)
    group-readable, and the ssh client refuses group-readable private keys, so a
    direct mount at ~/.ssh fails. A missing secret dir is not fatal here — the
    plan just fails later with a clearer ssh error.
    """
    if not secret_dir.is_dir():
        logger.warning("SSH secret dir %s missing; ssh will be unavailable", secret_dir)
        return
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    for entry in secret_dir.iterdir():
        # Secret volumes use ..data/ symlink indirection; skip dot-dirs.
        if entry.name.startswith(".") or not entry.is_file():
            continue
        target = ssh_dir / entry.name
        shutil.copyfile(entry, target)
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600 — ssh refuses looser


def _ssh_argv(host: str, command: str, *, user: str, key: str, timeout: int) -> List[str]:
    argv = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}"]
    if key:
        argv += ["-i", key]
    argv += [f"{user}@{host}", "--", command]
    return argv


class SSHError(RuntimeError):
    """SSH could not connect (transient/infra) — caller should let the lease reap."""


def run_ssh_plan(host: str, commands: List[str], env: Dict[str, str]) -> List[Dict[str, Any]]:
    """Run the validated commands on ``host`` over SSH, stopping on first failure.

    Returns a per-command result list. Raises SSHError on a connection failure
    (ssh exit 255) so the Job exits non-zero and the reaper retries; a command
    that connects but exits non-zero is reported, not raised (retry won't help).
    """
    secret_dir = (env.get("CFOP_SSH_SECRET_DIR") or "").strip()
    if secret_dir:
        prepare_ssh(Path(secret_dir), Path.home() / ".ssh")  # -> default identity
    # No default (CFOP-137). There is no defensible SSH username for an
    # arbitrary install, and the one that used to be here was a personal
    # account baked into a public image. The agent resolves this and passes it
    # in; an empty value means the deploy never said who to connect as, which
    # is a refusal, not a guess. Same posture as the empty AllowList.
    user = (env.get("CFOP_SSH_USER") or "").strip()
    if not user:
        raise SSHError(
            "no SSH user configured: set remediation.executor.node_action.ssh_user "
            "(the agent passes it as CFOP_SSH_USER)")
    key = (env.get("CFOP_SSH_KEY") or "").strip()  # optional explicit -i override
    timeout = int(env.get("CFOP_SSH_CONNECT_TIMEOUT", "5") or 5)
    cmd_timeout = int(env.get("CFOP_NODE_ACTION_TIMEOUT", "60") or 60)
    results: List[Dict[str, Any]] = []
    for command in commands:
        argv = _ssh_argv(host, command, user=user, key=key, timeout=timeout)
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=cmd_timeout)  # nosec - allowlisted cmd
        entry = {
            "command": command,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip()[:2000],
            "stderr": proc.stderr.strip()[:2000],
        }
        results.append(entry)
        if proc.returncode == 255:
            raise SSHError(f"ssh could not connect to {host}: {proc.stderr.strip()[:300]}")
        if proc.returncode != 0:
            logger.warning("command failed (rc=%s): %s", proc.returncode, command)
            break  # do not run later commands once one fails
    return results
