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
import requests as _requests
from .ssh import SSHTools
from .discovery import DiscoveryTools

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

        # Initialize SSH and discovery tools for fleet-wide access
        hosts_config = operator.config.get('infrastructure', {}).get('hosts', {})
        if hosts_config:
            self.ssh_tools = SSHTools(hosts_config)
            self.discovery_tools = DiscoveryTools(hosts_config)
            logger.info(f"SSH and discovery tools initialized for {len(hosts_config)} hosts")
        else:
            self.ssh_tools = None
            self.discovery_tools = None
            logger.warning("No infrastructure hosts configured - SSH/discovery tools disabled")

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

        # SSH tools for fleet-wide operations
        if self.ssh_tools:
            for schema in self.ssh_tools.get_schemas():
                tool_name = schema['name']
                self.tools[tool_name] = {
                    'function': self._make_ssh_tool_wrapper(tool_name),
                    'schema': schema
                }

        # Discovery tools for infrastructure verification
        if self.discovery_tools:
            for schema in self.discovery_tools.get_schemas():
                tool_name = schema['name']
                self.tools[tool_name] = {
                    'function': self._make_discovery_tool_wrapper(tool_name),
                    'schema': schema
                }

        # Knowledge base tools — allow the LLM to store and retrieve learnings
        self.tools['store_learning'] = {
            'function': self._store_learning,
            'schema': {
                'name': 'store_learning',
                'description': 'Save a learning/insight to the knowledge base. Use this when you diagnose an issue, the user tells you how they fixed something, or you discover a useful pattern. Learnings are reused in future investigations.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'learning_type': {
                            'type': 'string',
                            'description': 'Type: solution, pattern, root_cause, antipattern, or insight',
                            'enum': ['solution', 'pattern', 'root_cause', 'antipattern', 'insight']
                        },
                        'title': {
                            'type': 'string',
                            'description': 'Brief title (max 100 chars)'
                        },
                        'description': {
                            'type': 'string',
                            'description': 'Detailed description of what was learned and how to fix/avoid'
                        },
                        'applies_when': {
                            'type': 'string',
                            'description': 'Conditions when this learning applies'
                        },
                        'services': {
                            'type': 'array',
                            'items': {'type': 'string'},
                            'description': 'Services this applies to (e.g., ["immich-kiosk", "docker"])'
                        },
                        'tags': {
                            'type': 'array',
                            'items': {'type': 'string'},
                            'description': 'Tags for categorization (e.g., ["dns", "docker", "networking"])'
                        },
                        'category': {
                            'type': 'string',
                            'description': 'High-level category',
                            'enum': ['resource', 'network', 'config', 'dependency']
                        }
                    },
                    'required': ['learning_type', 'title', 'description']
                }
            }
        }

        self.tools['find_learnings'] = {
            'function': self._find_learnings,
            'schema': {
                'name': 'find_learnings',
                'description': 'Search the knowledge base for past learnings and solutions. Use this when investigating an issue to see if a similar problem was solved before.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'query': {
                            'type': 'string',
                            'description': 'Free-text search query (e.g., "docker dns failure")'
                        },
                        'services': {
                            'type': 'array',
                            'items': {'type': 'string'},
                            'description': 'Filter by services (e.g., ["immich-kiosk"])'
                        },
                        'category': {
                            'type': 'string',
                            'description': 'Filter by category: resource, network, config, or dependency'
                        },
                        'limit': {
                            'type': 'integer',
                            'description': 'Max results (default 5)',
                            'default': 5
                        }
                    }
                }
            }
        }

        # Web search tool (SearXNG)
        searxng_url = self.operator.config.get('search', {}).get('url', '')
        if searxng_url:
            self._searxng_url = searxng_url
            self.tools['web_search'] = {
                'function': self._web_search,
                'schema': {
                    'name': 'web_search',
                    'description': 'Search the web using SearXNG. Use this to look up documentation, error messages, software versions, CVEs, or any external information needed during investigations.',
                    'parameters': {
                        'type': 'object',
                        'properties': {
                            'query': {
                                'type': 'string',
                                'description': 'Search query (e.g., "docker dns resolution failure", "immich v1.99 changelog")'
                            }
                        },
                        'required': ['query']
                    }
                }
            }
            logger.info(f"Web search tool enabled (SearXNG: {searxng_url})")

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
            List of tool schemas in OpenAI function calling format
        """
        # Wrap each schema in OpenAI format (required by Ollama)
        return [
            {
                'type': 'function',
                'function': tool['schema']
            }
            for tool in self.tools.values()
        ]

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

    def _web_search(self, query: str) -> Dict[str, Any]:
        """Search the web using SearXNG."""
        try:
            resp = _requests.get(
                f"{self._searxng_url}/search",
                params={"q": query, "format": "json"},
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()

            results = data.get("results", [])[:5]
            if not results:
                return {'success': True, 'query': query, 'results': [], 'message': 'No results found'}

            return {
                'success': True,
                'query': query,
                'results': [
                    {
                        'title': r.get('title', ''),
                        'url': r.get('url', ''),
                        'content': r.get('content', '')[:300]
                    }
                    for r in results
                ]
            }
        except Exception as e:
            return {'error': str(e), 'query': query}

    def _store_learning(self, learning_type: str, title: str, description: str,
                        applies_when: str = '', services: List[str] = None,
                        tags: List[str] = None, category: str = '') -> Dict[str, Any]:
        """Store a learning in the knowledge base."""
        try:
            learning_data = {
                'learning_type': learning_type,
                'title': title[:100],
                'description': description,
                'applies_when': applies_when,
                'services': services or [],
                'tags': tags or [],
                'category': category,
            }
            lid = self.operator.kb.store_learning(learning_data)
            if lid and lid > 0:
                # Generate embedding for semantic search
                search_text = ' '.join(filter(None, [title, description, applies_when]))
                try:
                    self.operator._embed_learning(lid, search_text)
                except Exception:
                    pass  # Non-critical - FTS still works
                return {'success': True, 'learning_id': lid, 'title': title[:100]}
            else:
                return {'success': False, 'error': 'DB may be offline'}
        except Exception as e:
            return {'error': str(e)}

    def _find_learnings(self, query: str = '', services: List[str] = None,
                        category: str = '', limit: int = 5) -> Dict[str, Any]:
        """Search learnings in the knowledge base using hybrid (vector+FTS) search."""
        try:
            # Try hybrid search if we have a query and embeddings are available
            if query and hasattr(self.operator, 'embeddings') and self.operator.embeddings.is_available():
                query_embedding = self.operator.embeddings.generate_embedding(query)
                if query_embedding:
                    results = self.operator.kb._kb.find_learnings_hybrid(
                        query_text=query,
                        query_embedding=query_embedding,
                        limit=limit
                    )
                    # Apply service/category filters post-search if needed
                    if services:
                        results = [r for r in results if any(s in (r.get('services') or []) for s in services)]
                    if category:
                        results = [r for r in results if r.get('category') == category]
                else:
                    results = self.operator.kb.find_learnings(query=query, services=services, category=category, limit=limit)
            else:
                kwargs = {'limit': limit}
                if query:
                    kwargs['query'] = query
                if services:
                    kwargs['services'] = services
                if category:
                    kwargs['category'] = category
                results = self.operator.kb.find_learnings(**kwargs)
            return {
                'success': True,
                'count': len(results),
                'learnings': [
                    {
                        'id': r['id'],
                        'type': r['learning_type'],
                        'title': r['title'],
                        'description': r['description'][:300],
                        'applies_when': r.get('applies_when', ''),
                        'services': r.get('services', []),
                        'category': r.get('category', ''),
                        'success_rate': r.get('success_rate'),
                    }
                    for r in results
                ]
            }
        except Exception as e:
            return {'error': str(e), 'count': 0, 'learnings': []}

    def _make_ssh_tool_wrapper(self, tool_name: str):
        """Create wrapper function for SSH tools."""
        # Map tool names to SSHTools methods
        method_map = {
            'ssh_execute': 'execute',
            'ssh_check_service': 'check_service_status',
            'ssh_restart_service': 'restart_service',
            'ssh_get_logs': 'get_logs',
            'ssh_list_services': 'list_services',
            'ssh_docker_list': 'list_docker_containers',
            'ssh_docker_restart': 'docker_restart',
            'ssh_get_system_info': 'get_system_info',
            'ssh_check_port': 'check_port'
        }

        method_name = method_map.get(tool_name)
        if not method_name:
            return lambda **kwargs: {'error': f'Unknown SSH tool: {tool_name}'}

        method = getattr(self.ssh_tools, method_name)

        def wrapper(**kwargs):
            try:
                return method(**kwargs)
            except Exception as e:
                return {'error': str(e), 'tool': tool_name}

        return wrapper

    def _make_discovery_tool_wrapper(self, tool_name: str):
        """Create wrapper function for discovery tools."""
        # Map tool names to DiscoveryTools methods
        method_map = {
            'ping_host': 'ping_host',
            'verify_ssh': 'verify_ssh',
            'verify_sudo': 'verify_sudo',
            'discover_all_hosts': 'discover_all_hosts'
        }

        method_name = method_map.get(tool_name)
        if not method_name:
            return lambda **kwargs: {'error': f'Unknown discovery tool: {tool_name}'}

        method = getattr(self.discovery_tools, method_name)

        def wrapper(**kwargs):
            try:
                return method(**kwargs)
            except Exception as e:
                return {'error': str(e), 'tool': tool_name}

        return wrapper

__all__ = ['ToolRegistry']
