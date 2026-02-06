"""
Tool Registry for CFOperator
=============================

Provides infrastructure monitoring tools to the LLM:
- Prometheus metrics queries
- Loki log queries
- Docker container operations
- SSH remote execution
- System health checks

Tools from SRE Sentinel, adapted for single-agent architecture.
"""

from typing import Dict, Any, List, Optional
import logging

logger = logging.getLogger("cfoperator.tools")

class ToolRegistry:
    """
    Central registry for all tools available to CFOperator.

    Tools are loaded from individual modules and exposed to LLM
    with their schemas for function calling.
    """

    def __init__(self, operator):
        """
        Initialize tool registry with reference to operator.

        Args:
            operator: CFOperator instance for accessing config, backends
        """
        self.operator = operator
        self.tools = {}

        # Register all tools
        self._register_tools()

        logger.info(f"Tool registry initialized with {len(self.tools)} tools")

    def _register_tools(self):
        """Register all available tools."""
        # TODO: Import and register tools from modules
        # For now, register placeholder tools

        # Prometheus tools
        self.tools['prometheus_query'] = {
            'function': self._prometheus_query,
            'schema': {
                'name': 'prometheus_query',
                'description': 'Query Prometheus metrics across all monitored hosts',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'query': {
                            'type': 'string',
                            'description': 'PromQL query string'
                        }
                    },
                    'required': ['query']
                }
            }
        }

        # Loki tools
        self.tools['loki_query'] = {
            'function': self._loki_query,
            'schema': {
                'name': 'loki_query',
                'description': 'Query Loki logs across all monitored hosts',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'query': {
                            'type': 'string',
                            'description': 'LogQL query string'
                        },
                        'limit': {
                            'type': 'integer',
                            'description': 'Maximum number of log lines to return',
                            'default': 100
                        },
                        'since': {
                            'type': 'string',
                            'description': 'Time window (e.g., "1h", "24h")',
                            'default': '1h'
                        }
                    },
                    'required': ['query']
                }
            }
        }

        # Docker tools
        self.tools['docker_list'] = {
            'function': self._docker_list,
            'schema': {
                'name': 'docker_list',
                'description': 'List all Docker containers across all hosts',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'host': {
                            'type': 'string',
                            'description': 'Specific host to query (optional, queries all if not specified)'
                        }
                    }
                }
            }
        }

        self.tools['docker_inspect'] = {
            'function': self._docker_inspect,
            'schema': {
                'name': 'docker_inspect',
                'description': 'Inspect a specific Docker container',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'container_name': {
                            'type': 'string',
                            'description': 'Name of the container to inspect'
                        },
                        'host': {
                            'type': 'string',
                            'description': 'Host where container is running (optional)'
                        }
                    },
                    'required': ['container_name']
                }
            }
        }

    def execute(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a tool by name with given arguments.

        Args:
            tool_name: Name of the tool to execute
            arguments: Dictionary of arguments for the tool

        Returns:
            Tool execution result
        """
        if tool_name not in self.tools:
            return {'error': f'Tool {tool_name} not found'}

        try:
            logger.info(f"Executing tool: {tool_name}")
            func = self.tools[tool_name]['function']
            result = func(**arguments)
            logger.info(f"Tool {tool_name} completed successfully")
            return result
        except Exception as e:
            logger.error(f"Tool {tool_name} failed: {e}", exc_info=True)
            return {'error': str(e)}

    def get_schemas(self) -> List[Dict[str, Any]]:
        """
        Get tool schemas for LLM function calling.

        Returns:
            List of tool schemas in format expected by LLM
        """
        return [tool['schema'] for tool in self.tools.values()]

    # Tool implementations
    # ====================

    def _prometheus_query(self, query: str) -> Dict[str, Any]:
        """Query Prometheus metrics."""
        if not self.operator.metrics:
            return {'error': 'Prometheus backend not configured'}

        try:
            result = self.operator.metrics.query(query)
            return {
                'success': True,
                'query': query,
                'result': result
            }
        except Exception as e:
            return {'error': str(e)}

    def _loki_query(self, query: str, limit: int = 100, since: str = '1h') -> Dict[str, Any]:
        """Query Loki logs."""
        if not self.operator.logs:
            return {'error': 'Loki backend not configured'}

        try:
            result = self.operator.logs.query(query, since=since, limit=limit)
            return {
                'success': True,
                'query': query,
                'result': result
            }
        except Exception as e:
            return {'error': str(e)}

    def _docker_list(self, host: Optional[str] = None) -> Dict[str, Any]:
        """List Docker containers."""
        if not self.operator.containers:
            return {'error': 'Docker backend not configured'}

        try:
            containers = self.operator.containers.list_containers(host=host)
            return {
                'success': True,
                'host': host or 'all',
                'containers': containers
            }
        except Exception as e:
            return {'error': str(e)}

    def _docker_inspect(self, container_name: str, host: Optional[str] = None) -> Dict[str, Any]:
        """Inspect Docker container."""
        if not self.operator.containers:
            return {'error': 'Docker backend not configured'}

        try:
            info = self.operator.containers.inspect(container_name, host=host)
            return {
                'success': True,
                'container': container_name,
                'host': host,
                'info': info
            }
        except Exception as e:
            return {'error': str(e)}

__all__ = ['ToolRegistry']
