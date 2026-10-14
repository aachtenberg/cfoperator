"""
Infrastructure Discovery Tools
==============================

Auto-discover hosts, verify connectivity, and gather inventory.
"""

import re
import subprocess
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Tuple

# The agent's SSH identity (cfop-forensics, homelab-infra HOMELAB-28) is
# narrow by design: no `sudo` in general, NOPASSWD only for an allowlist of
# reads -- exact dmesg forms, du, read subcommands of crictl/docker/kubectl.
# `sudo -n echo` therefore fails on every host and says nothing. What an
# investigation depends on is the allowlist, so that is what is checked, by
# RUNNING each probe read with `sudo -n` (output discarded, 5s cap). Asking
# instead (`sudo -n -l <cmd>`) is not enough: under sudo's default
# listpw=any, listing succeeds for an entry that would still prompt for a
# password (CodeRabbit on #329). A refused probe is a real problem (the
# drop-in missing or broken, or -- found live -- sudo-rs resolving a
# symlinked binary out of the allowlist); full sudo being refused is the
# design (CFOP-391).
#
# Defaults for infrastructure.discovery.sudo_read_probes. Only reads that
# should run on every host belong in the default: dmesg -T. Probes are
# EXECUTED as root through the allowlist, so list only harmless reads.
SUDO_READ_PROBES = ('/usr/bin/dmesg -T',)
# A probe is spliced into a remote shell command, so only an absolute path
# and plain arguments are accepted -- no quoting, separators or expansions.
_SAFE_PROBE = re.compile(r'^/[\w./+-]+( [\w./=+-]+)*$')


def _sudo_checks(probes) -> str:
    reads = '; '.join(f'timeout 5 sudo -n {probe} >/dev/null 2>&1 && echo SUDO_READ_OK_{i}'
                      for i, probe in enumerate(probes))
    return f'sudo -n echo SUDO_OK 2>&1; {reads}'


def _refused_reads(probes, stdout: str) -> List[str]:
    """The probes whose OK marker is missing. Token match, not substring:
    SUDO_READ_OK_1 is a prefix of SUDO_READ_OK_10."""
    seen = set(stdout.split())
    return [probe for i, probe in enumerate(probes) if f'SUDO_READ_OK_{i}' not in seen]


_SUDO_CHECKS = _sudo_checks(SUDO_READ_PROBES)

# Hosts checked at once by discover_all_hosts. Each check is a ping and an
# SSH round trip, nearly all of it waiting on the network.
_DISCOVERY_WORKERS = 16

logger = logging.getLogger("cfoperator.tools.discovery")

class DiscoveryTools:
    """Tools for discovering and verifying infrastructure."""

    def __init__(self, hosts_config: Dict[str, Any], settings: Dict[str, Any] = None):
        """``settings`` is ``infrastructure.discovery``: ``sudo_read_probes``
        (allowlisted reads to run, a list or one string) and ``workers``
        (hosts checked at once). Both optional; see the module constants."""
        self.hosts = hosts_config
        settings = settings if isinstance(settings, dict) else {}
        configured = settings.get('sudo_read_probes')
        if isinstance(configured, str):
            configured = [configured]
        probes = []
        for probe in configured if isinstance(configured, (list, tuple)) else []:
            probe = str(probe).strip()
            if _SAFE_PROBE.match(probe):
                probes.append(probe)
            else:
                logger.warning(f"Ignoring sudo read probe {probe!r} in infrastructure.discovery: "
                               f"expected an absolute path and plain arguments")
        self.sudo_read_probes = tuple(probes) or SUDO_READ_PROBES
        self._sudo_checks = _sudo_checks(self.sudo_read_probes)
        try:
            workers = int(settings.get('workers') or _DISCOVERY_WORKERS)
        except (TypeError, ValueError):
            logger.warning(f"Ignoring infrastructure.discovery.workers={settings.get('workers')!r}")
            workers = _DISCOVERY_WORKERS
        self.workers = max(1, min(64, workers))
        logger.info(f"Discovery tools initialized for {len(self.hosts)} hosts")

    def ping_host(self, host: str) -> Dict[str, Any]:
        """Ping host to check if it's alive."""
        if host not in self.hosts:
            return {'success': False, 'error': f'Unknown host: {host}'}

        address = self.hosts[host]['address']

        try:
            # Three samples for the average, 0.2s apart (the floor iputils
            # allows without root) rather than the default 1s: the same
            # measurement in ~0.4s instead of ~2s (CFOP-391).
            result = subprocess.run(
                ['ping', '-c', '3', '-i', '0.2', '-W', '2', address],
                capture_output=True,
                text=True,
                timeout=10
            )

            alive = result.returncode == 0
            # Parse average latency from ping output
            latency = None
            if alive:
                for line in result.stdout.split('\n'):
                    if 'avg' in line or 'rtt' in line:
                        # Extract avg latency (varies by OS)
                        parts = line.split('/')
                        if len(parts) >= 5:
                            latency = float(parts[4])

            return {
                'success': True,
                'host': host,
                'address': address,
                'alive': alive,
                'latency_ms': latency,
                'output': result.stdout
            }
        except Exception as e:
            return {
                'success': False,
                'host': host,
                'address': address,
                'error': str(e)
            }

    def verify_ssh(self, host: str) -> Dict[str, Any]:
        """Verify SSH access to host."""
        if host not in self.hosts:
            return {'success': False, 'error': f'Unknown host: {host}'}

        host_config = self.hosts[host]
        ssh_user = host_config['ssh']['user']
        ssh_address = host_config['address']
        ssh_key = host_config['ssh'].get('key_path')

        # Build SSH command
        ssh_cmd = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null', '-o', 'ConnectTimeout=5']
        if ssh_key:
            ssh_cmd.extend(['-i', ssh_key])
        ssh_cmd.append(f'{ssh_user}@{ssh_address}')
        ssh_cmd.append('echo SSH_OK')

        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=10
            )

            ssh_ok = 'SSH_OK' in result.stdout
            return {
                'success': True,
                'host': host,
                'address': ssh_address,
                'ssh_accessible': ssh_ok,
                'error': result.stderr if not ssh_ok else None
            }
        except Exception as e:
            return {
                'success': False,
                'host': host,
                'address': ssh_address,
                'ssh_accessible': False,
                'error': str(e)
            }

    def verify_sudo(self, host: str) -> Dict[str, Any]:
        """Check sudo for the agent's SSH user: full passwordless sudo
        (``sudo_passwordless``) and the read-only allowlist investigations
        use (``sudo_reads``, with ``sudo_read_refused`` naming any probe
        read that would not run). The forensics identity is meant to have
        only the latter; see SUDO_READ_PROBES."""
        if host not in self.hosts:
            return {'success': False, 'error': f'Unknown host: {host}'}

        host_config = self.hosts[host]
        ssh_user = host_config['ssh']['user']
        ssh_address = host_config['address']
        ssh_key = host_config['ssh'].get('key_path')

        # Test sudo without password
        ssh_cmd = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null', '-o', 'ConnectTimeout=5']
        if ssh_key:
            ssh_cmd.extend(['-i', ssh_key])
        ssh_cmd.append(f'{ssh_user}@{ssh_address}')
        ssh_cmd.append(self._sudo_checks)

        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=10
            )

            sudo_ok = 'SUDO_OK' in result.stdout.split()
            needs_password = 'password is required' in result.stdout.lower()
            refused = _refused_reads(self.sudo_read_probes, result.stdout)

            return {
                'success': True,
                'host': host,
                'address': ssh_address,
                'sudo_passwordless': sudo_ok,
                'sudo_reads': not refused,
                'sudo_read_refused': refused,
                'needs_password': needs_password,
                'message': ' '.join(t for t in result.stdout.split()
                                    if not t.startswith('SUDO_READ_OK_')).strip()
            }
        except Exception as e:
            return {
                'success': False,
                'host': host,
                'error': str(e)
            }

    def _ssh_probe(self, host: str) -> Tuple[bool, bool, List[str]]:
        """(ssh_accessible, sudo_passwordless, refused read probes) from ONE
        connection.

        verify_ssh and verify_sudo each open their own, which is right for
        the single-purpose tools and wasteful for the inventory, where both
        questions go to every host (CFOP-391).
        """
        host_config = self.hosts[host]
        ssh_cmd = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null', '-o', 'ConnectTimeout=5']
        if host_config['ssh'].get('key_path'):
            ssh_cmd.extend(['-i', host_config['ssh']['key_path']])
        ssh_cmd.append(f"{host_config['ssh']['user']}@{host_config['address']}")
        ssh_cmd.append('echo SSH_OK; ' + self._sudo_checks)
        try:
            result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=10)
        except Exception as e:
            logger.debug(f"SSH probe of {host} failed: {e}")
            return False, False, list(self.sudo_read_probes)
        tokens = set(result.stdout.split())
        return ('SSH_OK' in tokens, 'SUDO_OK' in tokens,
                _refused_reads(self.sudo_read_probes, result.stdout))

    def _discover_host(self, host_name: str) -> Dict[str, Any]:
        """One inventory row: reachability, SSH and sudo for one host."""
        host_config = self.hosts[host_name]
        logger.info(f"Discovering host: {host_name}")
        ping_result = self.ping_host(host_name)
        ssh_ok, sudo_ok, refused = self._ssh_probe(host_name)

        host_info = {
            'name': host_name,
            'address': host_config['address'],
            'role': host_config.get('role', 'unknown'),
            'alive': ping_result.get('alive', False),
            'latency_ms': ping_result.get('latency_ms'),
            'ssh_accessible': ssh_ok,
            'sudo_passwordless': sudo_ok,
            'sudo_reads': ssh_ok and not refused,
            'monitoring': host_config.get('monitoring', []),
            'issues': []
        }

        # Collect issues
        if not host_info['alive']:
            host_info['issues'].append('Host not responding to ping')
        if not host_info['ssh_accessible']:
            host_info['issues'].append('SSH not accessible')
        # Full sudo is refused by design, so its absence is not an issue;
        # the read allowlist failing is. Only asked when SSH worked: with no
        # session there is no answer, and "SSH not accessible" already says so.
        if host_info['ssh_accessible'] and refused:
            host_info['issues'].append(
                f"Read-only sudo refused for: {', '.join(refused)}")
        return host_info

    def discover_all_hosts(self) -> Dict[str, Any]:
        """
        Discover and verify connectivity to all configured hosts.

        Returns comprehensive inventory of infrastructure. Hosts are checked
        at once rather than one after another: sequentially, seven healthy
        hosts took ~20s, every sweep (CFOP-391), and one dead host's timeouts
        stalled every host behind it. The inventory keeps config order.
        """
        names = list(self.hosts)
        if names:
            with ThreadPoolExecutor(max_workers=min(self.workers, len(names))) as pool:
                inventory = list(pool.map(self._discover_host, names))
        else:
            inventory = []

        # Summary
        total = len(inventory)
        alive = sum(1 for h in inventory if h['alive'])
        ssh_ok = sum(1 for h in inventory if h['ssh_accessible'])
        sudo_ok = sum(1 for h in inventory if h['sudo_passwordless'])
        sudo_reads = sum(1 for h in inventory if h['sudo_reads'])
        has_issues = sum(1 for h in inventory if h['issues'])

        return {
            'success': True,
            'total_hosts': total,
            'alive': alive,
            'ssh_accessible': ssh_ok,
            'sudo_passwordless': sudo_ok,
            'sudo_reads': sudo_reads,
            'hosts_with_issues': has_issues,
            'inventory': inventory
        }

    def get_schemas(self) -> List[Dict[str, Any]]:
        """Return tool schemas for LLM function calling."""
        return [
            {
                'name': 'ping_host',
                'description': 'Ping a host to check if it is alive and measure latency. Use this FIRST when troubleshooting connectivity.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'host': {
                            'type': 'string',
                            'description': f'Host to ping (one of: {", ".join(self.hosts.keys())})'
                        }
                    },
                    'required': ['host']
                }
            },
            {
                'name': 'verify_ssh',
                'description': 'Verify SSH access to a host. Use this to diagnose SSH connectivity issues.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'host': {
                            'type': 'string',
                            'description': 'Host to verify SSH access'
                        }
                    },
                    'required': ['host']
                }
            },
            {
                'name': 'verify_sudo',
                'description': ('Check sudo for the agent\'s SSH user on a host: sudo_reads is whether the read-only '
                                'allowlist investigations use (dmesg, du, crictl/docker/kubectl reads) works without '
                                'a password; sudo_passwordless is full sudo, which the agent\'s identity is meant NOT '
                                'to have. Host changes go through the node-action lane, not this user.'),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'host': {
                            'type': 'string',
                            'description': 'Host to verify sudo access'
                        }
                    },
                    'required': ['host']
                }
            },
            {
                'name': 'discover_all_hosts',
                'description': 'Discover and verify connectivity to ALL infrastructure hosts. Returns full inventory with status. Use this for infrastructure health checks.',
                'parameters': {
                    'type': 'object',
                    'properties': {}
                }
            }
        ]
