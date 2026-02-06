"""
Infrastructure Discovery Tools
==============================

Auto-discover hosts, verify connectivity, and gather inventory.
"""

import subprocess
import logging
from typing import Dict, Any, List

logger = logging.getLogger("cfoperator.tools.discovery")

class DiscoveryTools:
    """Tools for discovering and verifying infrastructure."""

    def __init__(self, hosts_config: Dict[str, Any]):
        self.hosts = hosts_config
        logger.info(f"Discovery tools initialized for {len(self.hosts)} hosts")

    def ping_host(self, host: str) -> Dict[str, Any]:
        """Ping host to check if it's alive."""
        if host not in self.hosts:
            return {'success': False, 'error': f'Unknown host: {host}'}

        address = self.hosts[host]['address']

        try:
            result = subprocess.run(
                ['ping', '-c', '3', '-W', '2', address],
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
        ssh_cmd = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=5']
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
        """Verify passwordless sudo on host."""
        if host not in self.hosts:
            return {'success': False, 'error': f'Unknown host: {host}'}

        host_config = self.hosts[host]
        ssh_user = host_config['ssh']['user']
        ssh_address = host_config['address']
        ssh_key = host_config['ssh'].get('key_path')

        # Test sudo without password
        ssh_cmd = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=5']
        if ssh_key:
            ssh_cmd.extend(['-i', ssh_key])
        ssh_cmd.append(f'{ssh_user}@{ssh_address}')
        ssh_cmd.append('sudo -n echo SUDO_OK 2>&1')

        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=10
            )

            sudo_ok = 'SUDO_OK' in result.stdout
            needs_password = 'password is required' in result.stdout.lower()

            return {
                'success': True,
                'host': host,
                'address': ssh_address,
                'sudo_passwordless': sudo_ok,
                'needs_password': needs_password,
                'message': result.stdout.strip()
            }
        except Exception as e:
            return {
                'success': False,
                'host': host,
                'error': str(e)
            }

    def discover_all_hosts(self) -> Dict[str, Any]:
        """
        Discover and verify connectivity to all configured hosts.

        Returns comprehensive inventory of infrastructure.
        """
        inventory = []

        for host_name, host_config in self.hosts.items():
            logger.info(f"Discovering host: {host_name}")

            # Check if alive
            ping_result = self.ping_host(host_name)

            # Check SSH access
            ssh_result = self.verify_ssh(host_name)

            # Check sudo
            sudo_result = self.verify_sudo(host_name)

            host_info = {
                'name': host_name,
                'address': host_config['address'],
                'role': host_config.get('role', 'unknown'),
                'alive': ping_result.get('alive', False),
                'latency_ms': ping_result.get('latency_ms'),
                'ssh_accessible': ssh_result.get('ssh_accessible', False),
                'sudo_passwordless': sudo_result.get('sudo_passwordless', False),
                'monitoring': host_config.get('monitoring', []),
                'issues': []
            }

            # Collect issues
            if not host_info['alive']:
                host_info['issues'].append('Host not responding to ping')
            if not host_info['ssh_accessible']:
                host_info['issues'].append('SSH not accessible')
            if not host_info['sudo_passwordless']:
                host_info['issues'].append('Sudo requires password')

            inventory.append(host_info)

        # Summary
        total = len(inventory)
        alive = sum(1 for h in inventory if h['alive'])
        ssh_ok = sum(1 for h in inventory if h['ssh_accessible'])
        sudo_ok = sum(1 for h in inventory if h['sudo_passwordless'])
        has_issues = sum(1 for h in inventory if h['issues'])

        return {
            'success': True,
            'total_hosts': total,
            'alive': alive,
            'ssh_accessible': ssh_ok,
            'sudo_passwordless': sudo_ok,
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
                'description': 'Verify passwordless sudo on a host. Use this to check if automated remediation will work.',
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
