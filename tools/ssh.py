"""
SSH Tools for Remote Host Management
=====================================

Provides tools for CFOperator to SSH into infrastructure hosts
for troubleshooting, log retrieval, service management, etc.
"""

import re
import subprocess
import json
import logging
import shlex
from typing import Dict, Any, Optional, List

logger = logging.getLogger("cfoperator.tools.ssh")

# HOMELAB-28: cfop-forensics is not in the docker group, on purpose. These
# helpers used to work only because the login (aachten) was. `sudo -n` uses
# the host sudoers, which allows the read subcommands and refuses the rest,
# and it fails closed instead of prompting.
_SUDO_DOCKER = "sudo -n docker"


def _q(value: Any) -> str:
    """Quote a value for interpolation into a remote shell command.

    Service, container and pattern names reach these helpers from LLM tool
    calls, so they are attacker-influenced whenever alert or log text is.
    Unquoted they would run as remote shell metacharacters (``systemctl
    restart 'a; curl ...'``).
    """
    return shlex.quote(str(value))


def _int(value: Any, default: int) -> int:
    """Coerce a numeric argument, falling back to the default when unusable."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Read-only classification for verification turns and unattended runs
# (CFOP-124, CFOP-240)
# ---------------------------------------------------------------------------
# A verify-only chat turn (a drawer or sweep-banner hand-off) and every
# unattended run (investigation, sweep) keep ssh_execute — the checks are ssh
# one-liners: mountpoint, systemctl status, journalctl, nc — but every command
# they send goes through this classifier, which refuses the mutators it knows.
# It is a denylist of known write shapes, not a proof of harmlessness: an
# interpreter one-liner (python -c, perl -e), a database client, or a script
# (sudo /usr/local/bin/docker-backup.sh, which an investigation ran on
# 2026-09-24) is not classified and runs. That gap is accepted here; what
# closes it is the SSH user not holding blanket sudo on the host. The role gate
# is separate and a member never gets ssh_execute at all.
#
# Since unattended runs go through it, a false positive refuses a real
# investigation read, so the reads it wrongly refused in 30 days of logs are
# pinned as tests: a '|' inside a quoted grep pattern, curl -o /dev/null.
_ENV_ASSIGN = re.compile(r"^(?:[A-Za-z_]\w*=\S*\s+)+")
# sudo/doas options that take an argument (-u root, -g adm, -D dir, ...) are
# consumed with it, or the argument is read as the program: `sudo -u root
# systemctl restart x` classified as a command named "root" and ran (CFOP-240,
# claude-review). The clustered form counts too: -nu root, -uroot, and the
# long form: --user root (CodeRabbit).
_SUDO_OPT = (r"(?:--(?:user|group|host|prompt|chdir|chroot|role|type|command-timeout|close-from|"
             r"other-user|login-class)\s+\S+|-[A-Za-z]*[ugUhpCDRrtT](?:\s+|(?=\S))\S+|-\S+)")
# The patterns below are constants built by concatenation, never from input,
# and each repetition alternates whitespace with non-whitespace, so matching
# stays linear in the command's length.
_WRAPPER = re.compile(
    r"^(?:sudo(?:\s+" + _SUDO_OPT + r")*|doas(?:\s+(?:-u\s*\S+|-\S+))*"
    # env/nice/timeout options that take a value, likewise (claude-review):
    # `timeout -s KILL 5 x` read KILL as the duration and 5 as the program.
    r"|env(?:\s+(?:-[uCP](?:\s+|(?=\S))\S+|--(?:unset|chdir)(?:=|\s+)\S+|-\S+|[A-Za-z_]\w*=\S*))*"
    r"|nice(?:\s+(?:-n\s*-?\d+|--adjustment(?:=|\s+)-?\d+|-{1,2}\d+))*|ionice(?:\s+-\S+)*"
    r"|timeout(?:\s+(?:-[sk](?:\s+|(?=\S))\S+|--(?:signal|kill-after)(?:=|\s+)\S+|-\S+))*\s+\S+"
    r"|command|exec|nohup|time|stdbuf(?:\s+-\S+)*"
    # Launchers that run their arguments as a command (CFOP-240, CodeRabbit).
    # A new launcher or option goes into LAUNCHER_PREFIXES in
    # tools/test_tool_policy.py in the same commit — an unhandled flag turns
    # its value into "the program" and lets a write through:
    # `xargs systemctl restart`, `chroot / systemctl stop x`, `nsenter -t 1 -m
    # -- systemctl restart kubelet`. Options that take a value are consumed
    # with it, or the value would be read as the program.
    r"|xargs(?:\s+(?:-[InPLsdEa](?:\s+|(?=\S))\S+|-\S+))*"
    r"|watch(?:\s+(?:-[ng](?:\s+|(?=\S))\S+|--interval(?:=|\s+)\S+|-\S+))*"
    r"|flock(?:\s+(?:-[wE](?:\s+|(?=\S))\S+|-(?![A-Za-z]*c\b)\S+))*\s+(?!-[A-Za-z]*c\b)\S+"
    r"|chroot(?:\s+-\S+)*\s+\S+|setsid(?:\s+-\S+)*|unshare(?:\s+-\S+)*"
    r"|nsenter(?:\s+(?:-[tSG](?:\s+|(?=\S))\S+|--(?:target|setuid|setgid)(?:=|\s+)\S+|-\S+))*"
    r"|runuser(?:\s+(?:-[ugG](?:\s+|(?=\S))\S+|-(?!-(?:\s|$))(?![A-Za-z]*c\b)\S+))*\s+--"
    r"|ssh(?:\s+(?:-[A-Za-z]*[BbcDEeFIiJLlmOoPpQRSWw](?:\s+|(?=\S))\S+|-\S+))*\s+\S+"
    r"|\\)\s+")
# A command handed over as one string: `sh -c '...'`, `bash -lc "..."`,
# `su - root -c '...'`, `runuser -l u -c '...'`, and what flock leaves once
# its file is consumed (`-c '...'`). The body is classified in turn.
_SHELL_C = re.compile(
    r"^(?:(?:(?:ba|z|da|k|a)?sh|su|runuser|flock)(?:\s+(?!-[A-Za-z]*c\b)\S+)*?\s+)?"
    r"-[A-Za-z]*c\s+(['\"])(.*)\1", re.S)
# A segment that is one quoted string once its launcher is stripped:
# `watch -n5 'systemctl restart x'`, `ssh pi2 'sudo reboot'`.
_WHOLLY_QUOTED = re.compile(r"^(['\"])(.*)\1$", re.S)
_PROGRAM_PATH = re.compile(r"^(?:\.{0,2}/)?(?:[\w.+-]+/)+(?=[\w.+-])")
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
# Device paths a write to is not a write: the bit bucket, the standard
# streams, and bash's /dev/tcp probes. Whole paths only — /dev/null.bak is a
# file (CFOP-240, claude-review).
_DEV_SINK = r"/dev/(?:null|stdout|stderr|stdin|fd/\d+|(?:tcp|udp)/\S+)(?![^\s;|&)])"
_REDIRECT = re.compile(r"(?<![<>&\d])>{1,2}\|?(?!\s*(?:&\s*[12]|" + _DEV_SINK + r"))")

_MUTATORS = [
    (re.compile(r"^systemctl\s+(?:--?\S+\s+)*(restart|stop|start|reload|reload-or-restart|try-restart|"
                r"enable|disable|mask|unmask|kill|daemon-reload|daemon-reexec|reset-failed|isolate|"
                r"set-default|set-property|revert|edit)\b"), "systemctl {0} changes service state"),
    (re.compile(r"^(reboot|shutdown|poweroff|halt|init\s+[06])\b"), "{0} takes the host down"),
    (re.compile(r"^(kill|pkill|killall)\b"), "{0} ends processes"),
    (re.compile(r"^(rm|rmdir|mv|cp|dd|truncate|shred|ln|chmod|chown|chgrp|touch|mkdir|mkfs\S*|fdisk|"
                r"parted|wipefs|swapoff|swapon)\b"), "{0} changes the filesystem"),
    (re.compile(r"^sed\b(?=.*(?:\s-[a-zA-Z]*i[a-zA-Z]*\b|\s--in-place))"), "sed -i edits a file in place"),
    (re.compile(r"^tee\b"), "tee writes a file"),
    (re.compile(r"^(apt|apt-get|dnf|yum|zypper|pacman|snap|pip3?|npm|brew)\s+(?:-\S+\s+)*"
                r"(install|remove|purge|upgrade|update|dist-upgrade|autoremove|uninstall|refresh)\b"),
     "{0} {1} changes installed packages"),
    (re.compile(r"^umount\b"), "umount unmounts a filesystem"),
    (re.compile(r"^mount\s+(?!-l\b)\S"), "mount with arguments mounts something"),
    (re.compile(r"^docker\s+(?:compose\s+)?(restart|stop|start|rm|rmi|kill|run|exec|up|down|prune|"
                r"pull|create|rename|update|pause|unpause|cp)\b"), "docker {0} changes container state"),
    (re.compile(r"^(?:kubectl|k3s\s+kubectl|oc)\s+(?:--?\S+\s+)*(apply|delete|patch|edit|scale|exec|cp|"
                r"drain|cordon|uncordon|taint|label|annotate|create|replace|rollout|set|expose|run)\b"),
     "kubectl {0} changes cluster state"),
    (re.compile(r"^(iptables|ip6tables)\b(?!.*\s-[LSC]\b)"), "{0} without -L changes firewall rules"),
    (re.compile(r"^nft\b(?!\s+list\b)"), "nft changes firewall rules"),
    (re.compile(r"^ufw\b(?!\s+status\b)"), "ufw changes firewall rules"),
    (re.compile(r"^crontab\s+(?!-l\b)"), "crontab edits scheduled jobs"),
    (re.compile(r"^(useradd|userdel|usermod|passwd|chpasswd|groupadd|groupdel|groupmod|visudo)\b"),
     "{0} changes accounts"),
    (re.compile(r"^git\s+(?:-C\s+\S+\s+)?(push|commit|reset|checkout|switch|rebase|merge|clean|stash|"
                r"pull|rm|mv|add|tag|restore)\b"), "git {0} changes the working tree or remote"),
    (re.compile(r"^(sysctl\s+(?:-w|\S+=)|modprobe|rmmod|insmod|hostnamectl\s+set|timedatectl\s+set|"
                r"nmcli\s+(?:con|connection|dev|device)\s+(?:up|down|mod|modify|del|delete|add)|"
                r"ip\s+(?:link|addr|address|route|neigh)\s+(?:add|del|delete|set|flush|change|replace))\b"),
     "{0} changes network or kernel state"),
    (re.compile(r"^(systemd-run|at|batch)\b"), "{0} schedules work on the host"),
    (re.compile(r"^journalctl\b(?=.*--(?:vacuum|rotate|flush))"), "journalctl --vacuum/--rotate changes the journal"),
    (re.compile(r"^find\b(?=.*\s(?:-delete\b|-exec\s+(?:rm|mv|chmod|chown|sed\s+-i)\b))"), "find -delete/-exec changes files"),
    # curl writes when it sends data or saves the body to a file. -o/--output
    # to a file counts, in any short-flag cluster (-so file, -sko file,
    # -ofile); -o /dev/null and -o - (stdout) do not, since a status probe that
    # discards its body is the commonest investigation read there is.
    (re.compile(r"^curl\b(?=.*\s(?:-X\s*(?:POST|PUT|DELETE|PATCH)\b|--request\s+(?:POST|PUT|DELETE|PATCH)\b|"
                r"-d\b|--data\S*|--json\b|-F\b|--form\S*|-T\b|--upload-file\b|"
                r"-[A-Za-z]*o(?:\s+|=?)(?!" + _DEV_SINK + r"|-(?:\s|$))\S|"
                r"--output(?:\s+|=)(?!" + _DEV_SINK + r"|-(?:\s|$))\S|-O\b|--remote-name\b))"),
     "curl that writes or sends data"),
    (re.compile(r"^wget\b(?!.*(?:-q?O\s*-|--spider))"), "wget writes a file"),
    # GPU management (CFOP-240). rocm-smi re-execs itself through sudo for any
    # set operation, so the program word alone never looked like a privileged
    # write; investigation 2558 ran --setfan 80 as root this way.
    (re.compile(r"^(rocm-smi)\b(?=.*\s(?:--(?:set\S*|reset\S*|gpureset|load|save|autorespond|"
                r"ras(?:enable|disable|inject))|-r)(?:[\s=]|$))"), "{0} changes GPU settings"),
    (re.compile(r"^(amd-smi)\s+(?:-\S+\s+)*(?:set|reset)\b"), "{0} changes GPU settings"),
    (re.compile(r"^(nvidia-smi)\b(?!\s+(?:dmon|pmon|topo)\b)(?=.*\s(?:-pl|-ac|-rac|-r|-pm|-c|-e|"
                r"-lgc|-rgc|-lmc|-rmc|-cgi|-dgi|-cci|-dci|--power-limit|--applications-clocks|"
                r"--reset-applications-clocks|--persistence-mode|--compute-mode|--ecc-config|"
                r"--gpu-reset|--lock-gpu-clocks|--reset-gpu-clocks|--lock-memory-clocks|"
                r"--reset-memory-clocks)(?:[\s=]|$))"), "{0} changes GPU settings"),
]


def _segments(text: str) -> List[str]:
    """Split a command line into the simple commands a shell would run.

    Quote-aware: a separator inside single quotes is text, and so is one inside
    double quotes — except ``$(`` and a backtick, which the shell still
    executes there. ``sh -c '...'`` stays one segment so its body is recursed
    into whole. Separators: ``;``, ``|``, ``||``, ``&&``, a background ``&``
    (not ``2>&1`` / ``&>``), newline, parentheses, ``$(`` and backticks.
    """
    out: List[str] = []
    buf: List[str] = []
    state = None          # None, "'" or '"'
    stack: List[tuple] = []  # (closer, state to restore) for $( ( and `

    def cut():
        seg = ''.join(buf).strip()
        if seg:
            out.append(seg)
        buf.clear()

    i, n = 0, len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ''
        if c == '\\' and state != "'":
            buf.append(text[i:i + 2])
            i += 2
            continue
        if state == "'":
            if c == "'":
                state = None
            buf.append(c)
        elif state == '"' and c == '"':
            state = None
            buf.append(c)
        elif c == '$' and nxt == '(':
            cut()
            stack.append((')', state))
            state = None
            i += 2
            continue
        elif c == '`':
            cut()
            if state is None and stack and stack[-1][0] == '`':
                state = stack.pop()[1]
            else:
                stack.append(('`', state))
                state = None
        elif state == '"':
            buf.append(c)
        elif c in '\'"':
            state = c
            buf.append(c)
        elif c == '(':
            cut()
            stack.append((')', None))
        elif c == ')':
            cut()
            if stack and stack[-1][0] == ')':
                state = stack.pop()[1]
        elif c in ';\n':
            cut()
        elif c == '|':
            cut()
            if nxt == '|':
                i += 1
        elif c == '&':
            if nxt == '&':
                cut()
                i += 1
            elif nxt == '>' or nxt.isdigit() or (buf and buf[-1] in '<>'):
                buf.append(c)
            else:
                cut()
        else:
            buf.append(c)
        i += 1
    cut()
    return out


def _unwrap(segment: str) -> str:
    """Strip sudo/env/launcher prefixes so the program word is first."""
    seg = segment.strip()
    while True:
        before = seg
        seg = _ENV_ASSIGN.sub('', seg)
        seg = _WRAPPER.sub('', seg)
        # The program by its name, not its path: /usr/bin/systemctl restart
        # and /opt/rocm/bin/rocm-smi --setfan are the same writes.
        seg = _PROGRAM_PATH.sub('', seg)
        if seg == before:
            return seg


# How many command-in-a-string layers are unwrapped (bash -c "su -c '...'").
# Real commands use two or three; past this the command is refused rather
# than recursed into, so a crafted `su -c ' -c ' -c ' ...` cannot exhaust
# the stack and raise out of the gate instead of answering (CFOP-240).
_MAX_NESTING = 8


def ssh_mutation_reason(command, _depth: int = 0) -> Optional[str]:
    """Why this shell command is not read-only, or None if nothing known matched.

    Every pipeline segment, subshell and ``sh -c`` body is unwrapped and its
    program word checked against the known mutators; output redirected to a
    file counts as a write (``2>&1``, ``/dev/null`` and ``/dev/tcp`` probes do
    not).
    """
    if _depth > _MAX_NESTING:
        return "the command nests too deeply to classify"
    text = str(command or '')
    for raw in _segments(text):
        seg = _unwrap(raw)
        if not seg:
            continue
        # A command handed over as a string (sh -c, su -c, ssh host '...',
        # watch '...') runs its body; the body is what gets classified.
        inner = _SHELL_C.match(seg) or _WHOLLY_QUOTED.match(seg)
        if inner:
            reason = ssh_mutation_reason(inner.group(2), _depth + 1)
            if reason:
                return reason
            continue
        for pattern, reason in _MUTATORS:
            m = pattern.match(seg)
            if m:
                return reason.format(*[g or '' for g in m.groups()])
        if _REDIRECT.search(_QUOTED.sub("''", seg)):
            return "output is redirected to a file"
    return None


class SSHTools:
    """
    SSH-based tools for remote host operations.

    CFOperator uses SSH to:
    - Execute commands on remote hosts
    - Check service status (systemd, docker)
    - Read log files
    - Restart services
    - Collect system metrics
    """

    def __init__(self, hosts_config: Dict[str, Any]):
        """
        Initialize SSH tools with host configuration.

        Args:
            hosts_config: Dict of host configs from config.yaml
        """
        self.hosts = hosts_config
        logger.info(f"SSH tools initialized for {len(self.hosts)} hosts")

    def execute(self, host: str, command: str, timeout: int = 30) -> Dict[str, Any]:
        """
        Execute command on remote host via SSH.

        Args:
            host: Hostname (must match key in hosts config)
            command: Shell command to execute
            timeout: Command timeout in seconds

        Returns:
            Dict with stdout, stderr, exit_code
        """
        if host not in self.hosts:
            return {
                'success': False,
                'error': f'Unknown host: {host}',
                'available_hosts': list(self.hosts.keys())
            }

        host_config = self.hosts[host]
        ssh_user = host_config['ssh']['user']
        ssh_address = host_config['address']
        ssh_key = host_config['ssh'].get('key_path')

        # Build SSH command
        ssh_cmd = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null']
        if ssh_key:
            ssh_cmd.extend(['-i', ssh_key])
        ssh_cmd.append(f'{ssh_user}@{ssh_address}')
        ssh_cmd.append(command)

        try:
            logger.info(f"Executing on {host}: {command}")
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )

            return {
                'success': result.returncode == 0,
                'stdout': result.stdout,
                'stderr': result.stderr,
                'exit_code': result.returncode,
                'host': host,
                'command': command
            }
        except subprocess.TimeoutExpired:
            return {
                'success': False,
                'error': f'Command timed out after {timeout}s',
                'host': host,
                'command': command
            }
        except Exception as e:
            return {
                'success': False,
                'error': str(e),
                'host': host,
                'command': command
            }

    def get_system_info(self, host: str) -> Dict[str, Any]:
        """Get basic system information from host."""
        result = self.execute(host, 'uname -a && uptime && df -h / && free -h')
        if result['success']:
            return {
                'success': True,
                'host': host,
                'info': result['stdout']
            }
        return result

    def check_service_status(self, host: str, service: str) -> Dict[str, Any]:
        """Check systemd service status on host."""
        result = self.execute(host, f'systemctl status {_q(service)}')
        return {
            'success': result['success'],
            'host': host,
            'service': service,
            'status': result['stdout'],
            'running': 'active (running)' in result['stdout'].lower()
        }

    def restart_service(self, host: str, service: str) -> Dict[str, Any]:
        """Restart systemd service on host."""
        result = self.execute(host, f'sudo systemctl restart {_q(service)}')
        if result['success']:
            # Verify restart succeeded
            status = self.check_service_status(host, service)
            return {
                'success': status['running'],
                'host': host,
                'service': service,
                'message': f"Service {service} restarted on {host}"
            }
        return result

    def get_logs(self, host: str, service: str = None, lines: int = 100) -> Dict[str, Any]:
        """Get logs from host (journalctl or docker logs)."""
        lines = _int(lines, 100)
        if service:
            # Try docker first, then journalctl
            docker_result = self.execute(host, f'{_SUDO_DOCKER} logs --tail {lines} {_q(service)} 2>&1')
            if docker_result['success']:
                return {
                    'success': True,
                    'host': host,
                    'service': service,
                    'logs': docker_result['stdout'],
                    'source': 'docker'
                }

            # Fall back to journalctl
            journal_result = self.execute(host, f'journalctl -u {_q(service)} -n {lines} --no-pager')
            return {
                'success': journal_result['success'],
                'host': host,
                'service': service,
                'logs': journal_result['stdout'],
                'source': 'journalctl'
            }
        else:
            # Get system logs
            result = self.execute(host, f'journalctl -n {lines} --no-pager')
            return {
                'success': result['success'],
                'host': host,
                'logs': result['stdout'],
                'source': 'journalctl'
            }

    def list_services(self, host: str) -> Dict[str, Any]:
        """List all running services on host — both Docker containers and systemd services."""
        services = []

        # Docker containers
        docker_result = self.execute(
            host, _SUDO_DOCKER + ' ps --format "{{.Names}}|{{.Status}}|{{.Image}}" 2>/dev/null')
        if docker_result['success']:
            for line in docker_result['stdout'].strip().split('\n'):
                if line:
                    parts = line.split('|')
                    if len(parts) == 3:
                        services.append({
                            'name': parts[0],
                            'type': 'container',
                            'status': parts[1],
                            'image': parts[2]
                        })

        # Systemd services (running only)
        systemd_result = self.execute(host, 'systemctl list-units --type=service --state=running --no-pager --no-legend')
        if systemd_result['success']:
            for line in systemd_result['stdout'].strip().split('\n'):
                if line:
                    parts = line.split()
                    if len(parts) >= 4:
                        svc_name = parts[0].replace('.service', '')
                        services.append({
                            'name': svc_name,
                            'type': 'systemd',
                            'status': 'running',
                            'description': ' '.join(parts[4:]) if len(parts) > 4 else ''
                        })

        return {
            'success': True,
            'host': host,
            'services': services,
            'containers': sum(1 for s in services if s['type'] == 'container'),
            'systemd': sum(1 for s in services if s['type'] == 'systemd')
        }

    def list_docker_containers(self, host: str) -> Dict[str, Any]:
        """List Docker containers on host."""
        result = self.execute(
            host, _SUDO_DOCKER + ' ps -a --format "{{.ID}}|{{.Names}}|{{.Status}}|{{.Image}}"')
        if result['success']:
            containers = []
            for line in result['stdout'].strip().split('\n'):
                if line:
                    parts = line.split('|')
                    if len(parts) == 4:
                        containers.append({
                            'id': parts[0],
                            'name': parts[1],
                            'status': parts[2],
                            'image': parts[3]
                        })
            return {
                'success': True,
                'host': host,
                'containers': containers,
                'count': len(containers)
            }
        return result

    def docker_inspect(self, host: str, container: str) -> Dict[str, Any]:
        """Get detailed info about Docker container on host."""
        result = self.execute(host, f'{_SUDO_DOCKER} inspect {_q(container)}')
        if result['success']:
            try:
                inspect_data = json.loads(result['stdout'])
                return {
                    'success': True,
                    'host': host,
                    'container': container,
                    'data': inspect_data[0] if inspect_data else {}
                }
            except json.JSONDecodeError:
                return {
                    'success': False,
                    'error': 'Failed to parse docker inspect output',
                    'host': host,
                    'container': container
                }
        return result

    def docker_restart(self, host: str, container: str) -> Dict[str, Any]:
        """Restart Docker container on host."""
        result = self.execute(host, f'{_SUDO_DOCKER} restart {_q(container)}')
        if result['success']:
            return {
                'success': True,
                'host': host,
                'container': container,
                'message': f"Container {container} restarted on {host}"
            }
        return result

    def get_disk_usage(self, host: str) -> Dict[str, Any]:
        """Get disk usage on host."""
        result = self.execute(host, 'df -h')
        return {
            'success': result['success'],
            'host': host,
            'output': result['stdout']
        }

    def get_memory_usage(self, host: str) -> Dict[str, Any]:
        """Get memory usage on host."""
        result = self.execute(host, 'free -h')
        return {
            'success': result['success'],
            'host': host,
            'output': result['stdout']
        }

    def get_process_list(self, host: str, filter_pattern: str = None) -> Dict[str, Any]:
        """Get process list on host."""
        cmd = 'ps aux'
        if filter_pattern:
            cmd += f' | grep -- {_q(filter_pattern)}'

        result = self.execute(host, cmd)
        return {
            'success': result['success'],
            'host': host,
            'processes': result['stdout'],
            'filter': filter_pattern
        }

    def check_port(self, host: str, port: int) -> Dict[str, Any]:
        """Check if port is listening on host."""
        port = _int(port, 0)
        result = self.execute(host, f'ss -tuln | grep -- {_q(f":{port} ")} || echo "NOT_LISTENING"')
        is_listening = 'NOT_LISTENING' not in result['stdout']
        return {
            'success': result['success'],
            'host': host,
            'port': port,
            'listening': is_listening,
            'output': result['stdout']
        }

    def get_network_connections(self, host: str) -> Dict[str, Any]:
        """Get active network connections on host."""
        result = self.execute(host, 'ss -tuln')
        return {
            'success': result['success'],
            'host': host,
            'connections': result['stdout']
        }

    def get_schemas(self) -> List[Dict[str, Any]]:
        """
        Return tool schemas for LLM function calling.

        These tools enable CFOperator to troubleshoot across the entire fleet.
        """
        return [
            {
                'name': 'ssh_execute',
                'mutating': True,  # CFOP-124: withheld from members and verify-only turns
                'description': 'Execute shell command on remote host via SSH. Use for troubleshooting, checking status, or any remote operation.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'host': {
                            'type': 'string',
                            'description': f'Target host (one of: {", ".join(self.hosts.keys())})'
                        },
                        'command': {
                            'type': 'string',
                            'description': 'Shell command to execute'
                        },
                        'timeout': {
                            'type': 'integer',
                            'description': 'Command timeout in seconds',
                            'default': 30
                        }
                    },
                    'required': ['host', 'command']
                }
            },
            {
                'name': 'ssh_check_service',
                'description': 'Check systemd service status on remote host',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'host': {'type': 'string', 'description': 'Target host'},
                        'service': {'type': 'string', 'description': 'Service name (e.g., docker, nginx)'}
                    },
                    'required': ['host', 'service']
                }
            },
            {
                'name': 'ssh_restart_service',
                'mutating': True,  # CFOP-124: withheld from members and verify-only turns
                'description': 'Restart systemd service on remote host (requires sudo)',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'host': {'type': 'string', 'description': 'Target host'},
                        'service': {'type': 'string', 'description': 'Service name to restart'}
                    },
                    'required': ['host', 'service']
                }
            },
            {
                'name': 'ssh_get_logs',
                'description': 'Get logs from remote host (docker logs or journalctl)',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'host': {'type': 'string', 'description': 'Target host'},
                        'service': {'type': 'string', 'description': 'Service/container name (optional)'},
                        'lines': {'type': 'integer', 'description': 'Number of lines', 'default': 100}
                    },
                    'required': ['host']
                }
            },
            {
                'name': 'ssh_list_services',
                'description': 'List ALL running services on a host — both Docker containers AND systemd services (e.g., ollama). Use this instead of ssh_docker_list when you want a complete picture.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'host': {'type': 'string', 'description': f'Target host (one of: {", ".join(self.hosts.keys())})'}
                    },
                    'required': ['host']
                }
            },
            {
                'name': 'ssh_docker_list',
                'description': 'List Docker containers on remote host (containers only, use ssh_list_services for full picture)',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'host': {'type': 'string', 'description': 'Target host'}
                    },
                    'required': ['host']
                }
            },
            {
                'name': 'ssh_docker_restart',
                'mutating': True,  # CFOP-124: withheld from members and verify-only turns
                'description': 'Restart Docker container on remote host',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'host': {'type': 'string', 'description': 'Target host'},
                        'container': {'type': 'string', 'description': 'Container name or ID'}
                    },
                    'required': ['host', 'container']
                }
            },
            {
                'name': 'ssh_get_system_info',
                'description': 'Get system info (uname, uptime, disk, memory) from remote host',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'host': {'type': 'string', 'description': 'Target host'}
                    },
                    'required': ['host']
                }
            },
            {
                'name': 'ssh_check_port',
                'description': 'Check if a port is listening on remote host',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'host': {'type': 'string', 'description': 'Target host'},
                        'port': {'type': 'integer', 'description': 'Port number to check'}
                    },
                    'required': ['host', 'port']
                }
            }
        ]
