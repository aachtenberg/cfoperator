"""
SSH Tools for Remote Host Management
=====================================

Provides tools for CFOperator to SSH into infrastructure hosts
for troubleshooting, log retrieval, service management, etc.
"""

import subprocess
import json
import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger("cfoperator.tools.ssh")

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
        result = self.execute(host, f'systemctl status {service}')
        return {
            'success': result['success'],
            'host': host,
            'service': service,
            'status': result['stdout'],
            'running': 'active (running)' in result['stdout'].lower()
        }

    def restart_service(self, host: str, service: str) -> Dict[str, Any]:
        """Restart systemd service on host."""
        result = self.execute(host, f'sudo systemctl restart {service}')
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
        if service:
            # Try docker first, then journalctl
            docker_result = self.execute(host, f'docker logs --tail {lines} {service} 2>&1')
            if docker_result['success']:
                return {
                    'success': True,
                    'host': host,
                    'service': service,
                    'logs': docker_result['stdout'],
                    'source': 'docker'
                }

            # Fall back to journalctl
            journal_result = self.execute(host, f'journalctl -u {service} -n {lines} --no-pager')
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
        docker_result = self.execute(host, 'docker ps --format "{{.Names}}|{{.Status}}|{{.Image}}" 2>/dev/null')
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
        result = self.execute(host, 'docker ps -a --format "{{.ID}}|{{.Names}}|{{.Status}}|{{.Image}}"')
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
        result = self.execute(host, f'docker inspect {container}')
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
        result = self.execute(host, f'docker restart {container}')
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
            cmd += f' | grep "{filter_pattern}"'

        result = self.execute(host, cmd)
        return {
            'success': result['success'],
            'host': host,
            'processes': result['stdout'],
            'filter': filter_pattern
        }

    def check_port(self, host: str, port: int) -> Dict[str, Any]:
        """Check if port is listening on host."""
        result = self.execute(host, f'ss -tuln | grep ":{port} " || echo "NOT_LISTENING"')
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
