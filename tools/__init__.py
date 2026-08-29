"""
Tool Registry for CFOperator
=============================

Provides infrastructure monitoring tools to the LLM:
- Prometheus metrics queries
- Loki log queries
- TimescaleDB read-only telemetry queries (MQTT device data)
- Docker container operations
- SSH remote execution
- System health checks

Tools for CFOperator's single-agent architecture.
"""

from dataclasses import dataclass
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import urlsplit
import ipaddress
import logging
import os
import re
import requests as _requests
from cfshared.config import ROLE_ADMIN
from .ssh import SSHTools, ssh_mutation_reason
from .discovery import DiscoveryTools
from .k8s import K8sTools
from .git import GitTools
from .github import GitHubTools
from .timescale import TimescaleTools

# Statuses that mean the executor holds a lease on the row. Mirrors
# _REMEDIATION_INFLIGHT in agent/knowledge_base.py, which cannot be imported
# here: agent/__init__.py pulls in agent.agent, whose bare `from
# knowledge_base import ...` only resolves with agent/ on sys.path. The copy
# is pinned to the original by test_inflight_statuses_match_the_queue.
_REMEDIATION_INFLIGHT = ('claimed', 'executing')

logger = logging.getLogger("cfoperator.tools")

# Tools that stay available on a verification turn even though they can write,
# because withholding them would leave a verification pass unable to verify:
# the sweep's own checks are ssh one-liners (mountpoint, systemctl status,
# journalctl, nc). Each command is classified instead — see ssh_mutation_reason.
# The ROLE gate is separate and stricter: a member never gets these at all.
_VERIFY_COMMAND_GATED = {'ssh_execute': 'command'}


@dataclass(frozen=True)
class ToolPolicy:
    """What one chat turn may do with the registry (CFOP-124).

    ``None`` in place of a policy is an internal caller — sweep, investigation,
    morning summary — and is unrestricted, exactly as before. A policy exists
    only for user-initiated chat: ``actor_role`` is the console role captured
    in ``POST /api/chat`` while request context still exists (the chat itself
    runs in a thread where it is gone), and ``verify_only`` marks a drawer or
    sweep-banner hand-off that asks for checks, not changes.

    Both layers consult it: ``get_schemas(policy)`` withholds mutating tools
    from what the model is offered, and ``execute(..., policy)`` refuses them
    anyway if the model names one regardless.
    """
    actor_role: Optional[str] = None
    verify_only: bool = False

    def role_allows_mutation(self) -> bool:
        """Whether the ASKER may change things at all, ignoring the turn's mode."""
        return self.actor_role is None or self.actor_role == ROLE_ADMIN

    def allows_mutation(self) -> bool:
        return self.role_allows_mutation() and not self.verify_only

    def allows_tool(self, tool_name: str, mutating: bool) -> bool:
        """Whether this turn may be OFFERED the tool at all.

        A verification turn keeps the command-gated tools (ssh_execute): the
        checks a hand-off asks for are ssh one-liners, and withholding the tool
        outright leaves the model narrating what it would have run. What it
        sends is classified at execute time instead. A member is refused them
        whatever the mode — that is the role gate, and it is not negotiable by
        the turn's purpose.
        """
        if not mutating:
            return True
        if self.allows_mutation():
            return True
        return (self.verify_only and self.role_allows_mutation()
                and tool_name in _VERIFY_COMMAND_GATED)

    def refusal(self, tool_name: str) -> str:
        """The error the model sees. Refusal, not escalation: it names what is
        needed and asks for the intent in words so an admin can act on it."""
        if self.verify_only and self.role_allows_mutation():
            return (f"{tool_name} changes the system, and this turn is a verification pass. "
                    "Report what you found and what should happen next; an operator "
                    "applies changes from the console.")
        return (f"{tool_name} changes the system; that needs an admin. Say exactly what "
                "you would run and why, so an admin can do it from the console.")

    def command_refusal(self, tool_name: str, reason: str) -> str:
        """The error for a read-only tool call carrying a command that writes."""
        return (f"{tool_name} is available on this verification turn for read-only checks, "
                f"but the command was refused: {reason}. Run the check that observes the "
                "state and report what should be done; an operator applies it.")

    def describe(self) -> str:
        return f"actor_role={self.actor_role!r} verify_only={self.verify_only}"


_NAMESPACE_FILE = '/var/run/secrets/kubernetes.io/serviceaccount/namespace'


def _own_namespace() -> Optional[str]:
    """The namespace this agent runs in, or None off-cluster / unknown."""
    for var in ('POD_NAMESPACE', 'CFOP_NAMESPACE'):
        value = os.getenv(var, '').strip()
        if value:
            return value.lower()
    try:
        with open(_NAMESPACE_FILE, encoding='utf-8') as fh:
            return fh.read().strip().lower() or None
    except OSError:
        return None


def _service_from_host(host: Optional[str]) -> Optional[Tuple[str, Optional[str], bool]]:
    """(service, namespace-or-None, ambiguous) for a host a pod could exec into.

    Kubernetes service DNS is ``svc``, ``svc.ns``, ``svc.ns.svc`` or
    ``svc.ns.svc.cluster.local``. An IP literal, localhost, or a name with
    three-plus labels that is not ``…svc…`` is not a pod in this cluster, so
    there is nothing for ``k8s_exec_pod`` to protect and the caller gets None,
    not an error.

    Two labels cannot be told apart lexically: ``kb-db.aweoriujwoedf`` is
    cluster DNS and ``timescale.local`` is a LAN name, and both parse the same
    way. Those come back with ``ambiguous=True`` and the caller settles it by
    asking the cluster whether that namespace exists — the only thing that
    actually distinguishes them. A bare service name resolves in the caller's
    own namespace; that is left None here and filled in by the registry.
    """
    h = str(host or '').strip().lower().rstrip('.')
    if h.startswith('['):            # [ipv6] or [ipv6]:port
        h = h[1:].split(']', 1)[0]
    elif h.count(':') == 1:          # host:port
        h = h.split(':', 1)[0]
    # More than one ':' without brackets is a bare IPv6 literal; it falls
    # through to the address check below.
    if not h or h == 'localhost':
        return None
    try:
        ipaddress.ip_address(h)
        return None
    except ValueError:
        pass
    labels = [label for label in h.split('.') if label]
    if len(labels) == 1:
        return labels[0], None, False
    if len(labels) == 2:
        return labels[0], labels[1], True
    if labels[2] == 'svc':
        return labels[0], labels[1], False
    return None

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

        # Initialize K8s tools (works in-cluster or with kubeconfig)
        k8s_config = operator.config.get('kubernetes', {})
        self.k8s_tools = K8sTools(
            kubeconfig=k8s_config.get('kubeconfig'),
            context=k8s_config.get('context')
        )
        logger.info("K8s tools initialized")

        # Initialize the repo-backed tools (GitHub API primary — repos
        # typically aren't cloned on deployed machines).
        self.github_tools = None
        self.git_tools = None
        self._init_git_tools()

        # Initialize TimescaleDB tools (read-only telemetry queries).
        # Env-driven like the knowledge-base connection; disabled without a
        # password so dev setups without the DB just lose the one tool.
        ts_password = os.getenv('TIMESCALE_PASSWORD', '')
        if ts_password:
            self.timescale_tools = TimescaleTools(
                host=os.getenv('TIMESCALE_HOST', 'timescaledb.data.svc.cluster.local'),
                port=int(os.getenv('TIMESCALE_PORT', '5432')),
                database=os.getenv('TIMESCALE_DB', 'sensors'),
                user=os.getenv('TIMESCALE_USER', 'cfoperator_ro'),
                password=ts_password,
            )
            logger.info("TimescaleDB tools initialized (read-only telemetry queries)")
        else:
            self.timescale_tools = None
            logger.warning("TIMESCALE_PASSWORD not set - timescale_query tool disabled")

        # Register all tools
        self._register_tools()
        self._check_marker_placement()

        logger.info(f"Tool registry initialized with {len(self.tools)} tools")

    # ------------------------------------------------------------------
    # Linked repos (CFOP-77)
    #
    # The repo registry is resolved DB-over-config by the operator and can
    # change while the process runs, so building and registering these two
    # tool families lives in helpers the console can call again rather than
    # inline in __init__.
    # ------------------------------------------------------------------

    def _build_git_tools(self) -> tuple:
        """Construct the tool objects for the current registry.

        Assigns nothing: a constructor that raises must leave the live tools
        alone rather than half-replace them (see refresh_git_tools).
        """
        git_config = self.operator.config.get('git', {}) or {}
        repos_config = git_config.get('repos', []) or []
        github_config = git_config.get('github', {}) or {}
        # Env fallback because the Helm ConfigMap templates no git block at
        # all: a repo linked from the console there would otherwise register
        # fine and still have no tools behind it. GITHUB_TOKEN is already how
        # the remediation PR client and the git context provider find the
        # credential — this reads the same one.
        github_token = str(github_config.get('token') or '').strip() or os.getenv('GITHUB_TOKEN', '').strip()
        github_tools = None
        if github_token and repos_config:
            github_tools = GitHubTools(
                token=github_token,
                api_url=github_config.get('api_url', 'https://api.github.com'),
                repos_config=repos_config,
            )
            logger.info(f"GitHub tools initialized for {len(repos_config)} repos")
        elif repos_config and not github_token:
            logger.warning("Repos are linked but no GitHub token is set - github_* tools disabled")

        # Local git tools are an optional supplement — only useful when a repo
        # clone exists on this host or is SSH-accessible.
        git_tools = None
        if repos_config and any(r.get("path") for r in repos_config):
            git_tools = GitTools(repos_config)
            logger.info(f"Local git tools initialized for {len(repos_config)} repos")
        return git_tools, github_tools

    def _init_git_tools(self) -> None:
        """(Re)build the GitHub API and local git tools from the registry."""
        self.git_tools, self.github_tools = self._build_git_tools()

    def _git_tool_names(self) -> list:
        """Tool names the current git/github instances contribute.

        Read off the live instances rather than matched by name prefix: these
        are the names that were actually registered, so unregistering cannot
        miss one or take out an unrelated tool that happens to start with
        ``git``.
        """
        names = []
        for tools in (self.git_tools, self.github_tools):
            if tools:
                names.extend(schema['name'] for schema in tools.get_schemas())
        return names

    def _register_git_tools(self) -> None:
        for tools, wrapper in ((self.git_tools, self._make_git_tool_wrapper),
                               (self.github_tools, self._make_github_tool_wrapper)):
            if not tools:
                continue
            for schema in tools.get_schemas():
                tool_name = schema['name']
                self.tools[tool_name] = {
                    'function': wrapper(tool_name),
                    'schema': schema,
                }

    def refresh_git_tools(self) -> Dict[str, Any]:
        """Rebuild the git/github tools after the registry changed.

        Unregistering is the load-bearing half: the repo names are baked into
        the tool *schema descriptions*, so an unlinked repo that keeps its
        tools registered stays advertised to the model — the exact staleness
        the console exists to remove.

        It happens *after* the replacements exist, though. Popping first and
        constructing second means a constructor that raises leaves the process
        linked to repos with no tools at all until something triggers another
        refresh — and the caller swallows the exception, so nothing would say
        so. Build, then swap.
        """
        git_tools, github_tools = self._build_git_tools()
        for name in self._git_tool_names():
            self.tools.pop(name, None)
        self.git_tools, self.github_tools = git_tools, github_tools
        self._register_git_tools()
        names = self._git_tool_names()
        logger.info(f"Git tools refreshed: {len(names)} tools for "
                    f"{len((self.operator.config.get('git') or {}).get('repos') or [])} repos")
        return {'tools': names}

    def _register_tools(self):
        """Register all available tools."""
        # TODO: Import and register tools from modules
        # For now, register placeholder tools

        # Prometheus tools
        self.tools['prometheus_query'] = {
            'function': self._prometheus_query,
            'schema': {
                'name': 'prometheus_query',
                'description': 'Query Prometheus metrics across all monitored hosts. '
                    'IMPORTANT: kube_node_status_condition produces 3 series per node (status=true/false/unknown). '
                    'To count Ready nodes use: count(kube_node_status_condition{condition="Ready", status="true"} == 1)',
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
                'description': 'Query Loki logs across all monitored hosts. '
                    'Available labels: host, container_name, compose_service, container, container_id, job, level, source, stream, component, service_name. '
                    'Host values: raspberrypi, raspberrypi2, raspberrypi3, raspberrypi4, headless-gpu. '
                    'CORRECT syntax examples: '
                    '(1) {host="raspberrypi2"} |= "error"  '
                    '(2) {container_name="immich_server"} |= "error"  '
                    '(3) {host="raspberrypi2", container_name="telegraf"} |= "timeout"  '
                    '(4) {container_name=~"immich.*"} |= "error"  '
                    '(5) {host=~"raspberrypi|raspberrypi2|raspberrypi3"} |= "error" (regex for multiple hosts).  '
                    'WRONG patterns (DO NOT USE): '
                    '{job="x"} |= "e" and {container_name="y"} is WRONG - combine into {job="x", container_name="y"} |= "e".  '
                    '{host!="x"} |~ "error" is WRONG - negative-only selectors are rejected by Loki.  '
                    '{sel1} || {sel2} or {sel1} -- {sel2} is WRONG - LogQL has no multi-query syntax. Make separate calls.  '
                    '{host="a",b,c} is WRONG - use {host=~"a|b|c"} for multiple values.  '
                    'ALWAYS include at least one positive matcher (= or =~) in the selector. '
                    'Each call must contain exactly ONE stream selector {}. '
                    'Never use and/or/||/-- between {} selectors. Never quote the selector. Use regex .* not glob *.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'query': {
                            'type': 'string',
                            'description': 'LogQL query. Put ALL labels in one {} selector. Example: {host="raspberrypi2", container_name="telegraf"} |= "error". Never use and/or between {} selectors. Use regex .* not glob *.'
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

        # K8s tools for cluster operations
        if self.k8s_tools:
            for schema in self.k8s_tools.get_schemas():
                tool_name = schema['name']
                self.tools[tool_name] = {
                    'function': self._make_k8s_tool_wrapper(tool_name),
                    'schema': schema
                }

        # Git + GitHub tools for code-change investigation and PR/issue
        # operations. Registered through the same helper the console's
        # relink path uses, so there is one definition of what a repo
        # change adds to the tool set.
        self._register_git_tools()

        # TimescaleDB read-only telemetry queries
        if self.timescale_tools:
            for schema in self.timescale_tools.get_schemas():
                tool_name = schema['name']
                self.tools[tool_name] = {
                    'function': self._make_timescale_tool_wrapper(tool_name),
                    'schema': schema
                }

        # Knowledge base tools — allow the LLM to store and retrieve learnings
        self.tools['store_learning'] = {
            'function': self._store_learning,
            'schema': {
                'name': 'store_learning',
                # A learning is read by future sweeps and investigations and can
                # act as a suppression — it decides what the system will and
                # will not act on, the same test the admin gate on
                # PATCH /api/findings cites.
                'mutating': True,
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

        # Sweep report tools — allow the LLM to view and update sweep findings
        self.tools['get_sweep_report'] = {
            'function': self._get_sweep_report,
            'schema': {
                'name': 'get_sweep_report',
                'description': 'Get a sweep report by ID. Returns findings with their index numbers. Use this when a user references a sweep report number (e.g., "#42").',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'report_id': {
                            'type': 'integer',
                            'description': 'The sweep report ID number'
                        }
                    },
                    'required': ['report_id']
                }
            }
        }

        self.tools['update_sweep_finding'] = {
            'function': self._update_sweep_finding,
            'schema': {
                'name': 'update_sweep_finding',
                'mutating': True,  # its HTTP twin PATCH /api/findings is admin-gated
                'description': 'Update the status of a specific finding in a sweep report. Use finding_id (preferred) or finding_index to identify the finding.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'report_id': {
                            'type': 'integer',
                            'description': 'The sweep report ID number'
                        },
                        'finding_id': {
                            'type': 'string',
                            'description': 'Stable finding ID (preferred over finding_index)'
                        },
                        'finding_index': {
                            'type': 'integer',
                            'description': 'Index of the finding within the report (0-based). Fallback if finding_id not provided.'
                        },
                        'status': {
                            'type': 'string',
                            'description': 'New status for the finding',
                            'enum': ['resolved', 'acknowledged', 'investigating', 'false_positive']
                        },
                        'resolution': {
                            'type': 'string',
                            'description': 'Optional note explaining the resolution or action taken'
                        }
                    },
                    'required': ['report_id', 'status']
                }
            }
        }

        # Operational data tools — let LLM query sweep stats, investigations, correlations
        self.tools['get_operational_summary'] = {
            'function': self._get_operational_summary,
            'schema': {
                'name': 'get_operational_summary',
                'description': 'Get aggregate statistics about CFOperator activity — sweep counts, finding averages, investigation outcomes, and learnings. Use this when asked about operational metrics, sweep history, how many sweeps or findings, or any "how many" questions about agent activity.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'hours': {
                            'type': 'integer',
                            'description': 'How many hours back to look (default 24)'
                        }
                    },
                    'required': []
                }
            }
        }

        self.tools['list_sweep_reports'] = {
            'function': self._list_sweep_reports,
            'schema': {
                'name': 'list_sweep_reports',
                'description': 'List recent sweep reports with findings summaries. Use this to browse sweeps, see what was found, and get report IDs for deeper investigation with get_sweep_report.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'limit': {
                            'type': 'integer',
                            'description': 'Number of reports to return (default 10)'
                        }
                    },
                    'required': []
                }
            }
        }

        self.tools['list_investigations'] = {
            'function': self._list_investigations,
            'schema': {
                'name': 'list_investigations',
                'description': 'List recent investigations with triggers and outcomes. Use this to see what the agent has investigated, whether issues were resolved, and how long investigations took.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'limit': {
                            'type': 'integer',
                            'description': 'Number of investigations to return (default 10)'
                        }
                    },
                    'required': []
                }
            }
        }

        self.tools['get_investigation'] = {
            'function': self._get_investigation,
            'schema': {
                'name': 'get_investigation',
                'description': 'Fetch a single investigation by id (trigger, findings, outcome, duration). Use when following an investigation_id from a remediation or when the operator asks about a specific investigation.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'investigation_id': {
                            'type': 'integer',
                            'description': 'Investigation id to fetch'
                        }
                    },
                    'required': ['investigation_id']
                }
            }
        }

        self.tools['list_remediations'] = {
            'function': self._list_remediations,
            'schema': {
                'name': 'list_remediations',
                'description': 'List remediation queue rows (newest first). Optionally filter by status (queued, needs-human, pr-open, resolved, …). Use this to answer questions about the remediation queue.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'status': {
                            'type': 'string',
                            'description': 'Optional status filter (e.g. needs-human, queued, pr-open)'
                        },
                        'limit': {
                            'type': 'integer',
                            'description': 'Max rows to return (default 20)'
                        }
                    },
                    'required': []
                }
            }
        }

        self.tools['get_remediation'] = {
            'function': self._get_remediation,
            'schema': {
                'name': 'get_remediation',
                'description': 'Fetch a single remediation queue row by id, including recommendation, payload (provider, proposed_diff, pr_attempt), status, and linked investigation_id.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'remediation_id': {
                            'type': 'integer',
                            'description': 'Remediation queue id to fetch'
                        }
                    },
                    'required': ['remediation_id']
                }
            }
        }

        # The one WRITE tool for the remediation queue. It exists because
        # without it an operator saying "resolve it" left the agent holding
        # only update_sweep_finding — so it closed a sweep finding, reported
        # that truthfully, and the remediation stayed on /remediations
        # (CFOP-123). Mirrors POST /api/remediations/<id>/resolve.
        self.tools['resolve_remediation'] = {
            'function': self._resolve_remediation,
            'schema': {
                'name': 'resolve_remediation',
                'mutating': True,
                'description': (
                    'Close a remediation queue row when the operator says the work is done, '
                    'no longer needed, or the device/service is decommissioned — or approve it, '
                    'handing it to the executor. This is the ONLY way to take a row off the '
                    '/remediations worklist or send it on — update_sweep_finding does NOT close a '
                    'remediation. Use get_remediation first to confirm the id.'
                ),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'remediation_id': {
                            'type': 'integer',
                            'description': 'Remediation queue id to close'
                        },
                        'status': {
                            'type': 'string',
                            'description': ("'resolved' when the underlying problem is handled or moot; "
                                            "'rejected' when the proposed fix itself was wrong; "
                                            "'approved' to queue it for the executor (the console's Approve)"),
                            'enum': ['resolved', 'rejected', 'approved']
                        },
                        'note': {
                            'type': 'string',
                            'description': ('Why it is being closed, in the operator\'s terms '
                                            '(e.g. "device permanently decommissioned"). Recorded on the row. '
                                            'Required to resolve or reject; optional to approve.')
                        }
                    },
                    'required': ['remediation_id']
                }
            }
        }

        self.tools['get_correlations'] = {
            'function': self._get_correlations,
            'schema': {
                'name': 'get_correlations',
                'description': 'Find correlated events — investigations and drift events that occurred close together, service failure patterns, and learned service dependencies. Use this to understand relationships between incidents.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'hours': {
                            'type': 'integer',
                            'description': 'How many hours back to look for correlations (default 24)'
                        }
                    },
                    'required': []
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

    def _check_marker_placement(self) -> None:
        """Fail registration if a ``mutating`` marker is in the wrong home.

        There is one home: inside the tool's own schema, beside its name —
        where a family (tools/ssh.py, tools/k8s.py, tools/github.py) already
        puts it. An entry-level key would be silently read as False by
        is_mutating and leave a write tool open, which is the whole defect
        this issue is about, so it stops the process rather than shipping.
        """
        misplaced = sorted(name for name, entry in self.tools.items() if 'mutating' in entry)
        if misplaced:
            raise ValueError(
                "'mutating' belongs inside the tool's schema, not on the registry entry: "
                + ', '.join(misplaced))

    def is_mutating(self, tool_name: str) -> bool:
        """True if the tool changes the system, or what the system will act on."""
        entry = self.tools.get(tool_name)
        return bool(entry and (entry.get('schema') or {}).get('mutating'))

    def execute(self, tool_name: str, arguments: Dict[str, Any],
                policy: Optional[ToolPolicy] = None) -> Dict[str, Any]:
        """
        Execute a tool by name with given arguments.

        Args:
            tool_name: Name of the tool to execute
            arguments: Dictionary of arguments for the tool
            policy: What this turn may do (CFOP-124); None is unrestricted

        Returns:
            Tool execution result
        """
        if tool_name not in self.tools:
            return {'error': f'Tool {tool_name} not found'}
        if policy is not None and self.is_mutating(tool_name):
            if not policy.allows_tool(tool_name, True):
                # Defence in depth: get_schemas(policy) already withheld this
                # tool from the model. A model that names it anyway is refused.
                logger.warning(f"Tool {tool_name} refused ({policy.describe()})")
                return {'error': policy.refusal(tool_name), 'refused': True, 'tool': tool_name}
            if not policy.allows_mutation():
                # Offered only because it is command-gated (verify turn): the
                # tool may run, this particular command may not.
                arg = _VERIFY_COMMAND_GATED.get(tool_name)
                reason = ssh_mutation_reason((arguments or {}).get(arg)) if arg else None
                if reason:
                    logger.warning(f"Tool {tool_name} command refused ({policy.describe()}): {reason}")
                    return {'error': policy.command_refusal(tool_name, reason),
                            'refused': True, 'tool': tool_name}

        try:
            # Defensive: handle arguments that may still be JSON strings
            if isinstance(arguments, str):
                import json
                arguments = json.loads(arguments) if arguments.strip() else {}
            elif arguments is None:
                arguments = {}

            logger.info(f"Executing tool: {tool_name}")
            func = self.tools[tool_name]['function']
            result = func(**arguments)
            logger.info(f"Tool {tool_name} completed successfully")
            return result
        except Exception as e:
            logger.error(f"Tool {tool_name} failed: {e}", exc_info=True)
            return {'error': str(e)}

    def get_schemas(self, policy: Optional[ToolPolicy] = None) -> List[Dict[str, Any]]:
        """
        Get tool schemas for LLM function calling.

        Args:
            policy: What this turn may do (CFOP-124). Mutating tools are left
                out when the policy does not allow them, so the model is never
                offered — or told about — a tool that would refuse. None
                returns everything.

        Returns:
            List of tool schemas in OpenAI function calling format
        """
        # The marker lives in the schema (one home), so it is stripped here on
        # the way to the model: a stray key is a 400 on the stricter providers.
        return [
            {
                'type': 'function',
                'function': {k: v for k, v in tool['schema'].items() if k != 'mutating'}
            }
            for name, tool in self.tools.items()
            if policy is None or policy.allows_tool(name, bool(tool['schema'].get('mutating')))
        ]

    # Tool implementations
    # ====================

    def _prometheus_query(self, query: str) -> Dict[str, Any]:
        """Query Prometheus metrics."""
        if not self.operator.metrics:
            return {'error': 'Prometheus backend not configured'}

        # Auto-correct: kube_node_status_condition needs status="true" filter
        # to avoid counting all 3 series per node (true/false/unknown)
        if 'kube_node_status_condition' in query and 'status=' not in query:
            query = query.replace(
                'kube_node_status_condition{',
                'kube_node_status_condition{status="true", '
            )
            logger.warning(f"Auto-corrected PromQL query to include status=\"true\": {query}")

        try:
            result = self.operator.metrics.query(query)
            return {
                'success': True,
                'query': query,
                'result': result
            }
        except Exception as e:
            return {'error': str(e)}

    def _loki_query(self, query: str = None, limit: int = 100, since: str = '1h', expr: str = None) -> Dict[str, Any]:
        """Query Loki logs with input validation."""
        # Accept 'expr' as alias for 'query' (LLMs often use Prometheus naming)
        if query is None and expr is not None:
            query = expr
        if not query:
            return {'error': 'Missing required parameter: query'}
        if not self.operator.logs:
            return {'error': 'Loki backend not configured'}

        # Strip accidental whitespace/quotes
        query = query.strip().strip("'").strip('"')
        logger.debug(f"Loki query: {query} (since={since}, limit={limit})")

        try:
            result = self.operator.logs.query(query, since=since, limit=limit)
            logger.debug(f"Loki query returned {len(result)} streams")
            return {
                'success': True,
                'query': query,
                'result': result
            }
        except ValueError as e:
            # Validation error - return helpful hint
            logger.warning(f"Loki query rejected: {e} | query: {query}")
            return {
                'error': str(e),
                'hint': 'Put all labels in ONE selector: {label1="val1", label2="val2"} |= "filter". Use regex .* not glob *. Do not quote the selector.'
            }
        except Exception as e:
            logger.warning(f"Loki query failed: {e} | query: {query}")
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

    def _get_sweep_report(self, report_id: int) -> Dict[str, Any]:
        """Get a sweep report by ID."""
        try:
            report = self.operator.kb.get_sweep_report(report_id)
            if not report:
                return {'error': f'Sweep report #{report_id} not found'}
            # Add index to each finding for easy reference
            for i, f in enumerate(report.get('findings', [])):
                f['index'] = i
            return {'success': True, 'report': report}
        except Exception as e:
            return {'error': str(e)}

    def _update_sweep_finding(self, report_id: int, finding_index: int = -1,
                              status: str = '', resolution: str = '',
                              finding_id: str = '') -> Dict[str, Any]:
        """Update a finding's status in a sweep report."""
        if not finding_id and finding_index < 0:
            return {'error': 'Must provide finding_id or finding_index (0-based). '
                    'Use get_sweep_report to list findings and their IDs/indices first.'}
        if self._is_morning_summary_report(report_id):
            # A morning summary is stored as a sweep report carrying ONE
            # synthetic finding whose body is the whole digest (agent.py,
            # _send_morning_summary). There is no per-subject finding to
            # close, so resolving index 0 silently closes every issue the
            # digest mentions — which is what happened in CFOP-123.
            return {'error': f'Report #{report_id} is a morning summary, not a sweep. Its single '
                    'finding is the entire digest, so closing it would close every issue the '
                    'summary mentions. To act on one item, close its remediation with '
                    'resolve_remediation (find it via list_remediations).'}
        try:
            updated = self.operator.kb.update_sweep_finding(
                report_id, finding_index=finding_index, status=status,
                resolution=resolution, finding_id=finding_id
            )
            ref = finding_id or f"index {finding_index}"
            if updated:
                return {'success': True, 'report_id': report_id,
                        'finding': ref, 'status': status}
            else:
                return {'error': f'Finding {ref} not found in report #{report_id}. '
                        'Use get_sweep_report to list valid finding IDs/indices.'}
        except Exception as e:
            return {'error': str(e)}

    def _get_operational_summary(self, hours: int = 24) -> Dict[str, Any]:
        """Get aggregate operational statistics."""
        try:
            summary = self.operator.kb.get_operational_summary(hours)
            return {'success': True, **summary}
        except Exception as e:
            return {'error': str(e)}

    def _list_sweep_reports(self, limit: int = 10) -> Dict[str, Any]:
        """List recent sweep reports."""
        try:
            reports = self.operator.kb.get_recent_sweep_reports(limit)
            for r in reports:
                if r.get('summary') and len(r['summary']) > 200:
                    r['summary'] = r['summary'][:200] + '...'
            return {'success': True, 'count': len(reports), 'reports': reports}
        except Exception as e:
            return {'error': str(e)}

    def _list_investigations(self, limit: int = 10) -> Dict[str, Any]:
        """List recent investigations."""
        try:
            investigations = self.operator.kb.get_recent_investigations(limit)
            return {'success': True, 'count': len(investigations), 'investigations': investigations}
        except Exception as e:
            return {'error': str(e)}

    def _get_investigation(self, investigation_id: int) -> Dict[str, Any]:
        """Fetch one investigation by id for chat follow-ups."""
        try:
            inv = self.operator.kb.get_investigation(int(investigation_id))
            if not inv:
                return {'error': f'Investigation #{investigation_id} not found'}
            return {'success': True, 'investigation': self._trim_investigation_for_tool(inv)}
        except Exception as e:
            return {'error': str(e)}

    def _list_remediations(self, status: str = '', limit: int = 20) -> Dict[str, Any]:
        """List remediation queue rows for the chat agent (read-only)."""
        try:
            lim = max(1, min(int(limit or 20), 50))
            status_filter = (status or '').strip() or None
            rows = self.operator.kb.list_remediations(status=status_filter, limit=lim)
            trimmed = [self._trim_remediation_for_tool(r) for r in rows]
            return {'success': True, 'count': len(trimmed), 'remediations': trimmed}
        except Exception as e:
            return {'error': str(e)}

    def _get_remediation(self, remediation_id: int) -> Dict[str, Any]:
        """Fetch one remediation by id for the chat agent (read-only)."""
        try:
            row = self.operator.kb.get_remediation(int(remediation_id))
            if not row:
                return {'error': f'Remediation #{remediation_id} not found'}
            return {'success': True, 'remediation': self._trim_remediation_for_tool(row)}
        except Exception as e:
            return {'error': str(e)}

    def _resolve_remediation(self, remediation_id: int = None, note: str = '',
                             status: str = 'resolved') -> Dict[str, Any]:
        """Close a remediation queue row on the operator's say-so.

        The chat twin of POST /api/remediations/<id>/resolve. Writes the same
        ``result`` keys the console route writes so /remediations renders a
        chat-closed row identically to a button-closed one.
        """
        if status not in ('resolved', 'rejected', 'approved'):
            return {'error': f"status must be 'resolved', 'rejected' or 'approved', got {status!r}"}
        note = (note or '').strip()
        if status != 'approved' and not note:
            return {'error': 'A note is required — it is the only record of why this was closed.'}
        if remediation_id is None:
            return {'error': 'remediation_id is required. Use list_remediations to find it.'}
        try:
            rid = int(remediation_id)
        except (TypeError, ValueError):
            return {'error': f'remediation_id must be an integer, got {remediation_id!r}'}
        try:
            row = self.operator.kb.get_remediation(rid)
            if not row:
                return {'error': f'Remediation #{rid} not found. Use list_remediations to see open rows.'}

            # Refuse rows the executor is mid-flight on: it still holds the
            # lease and will POST /v1/remediations/<id>/complete when its Job
            # ends, against a row that has moved on underneath it.
            #
            # Keyed on status, NOT on claimed_at: update_remediation_status
            # never clears claimed_at, and only stamps completed_at for the
            # terminal three — so a finished 'pr-open' / 'verifying' row still
            # looks claimed-and-incomplete. That is exactly the row an operator
            # asks chat to close once the PR exists, so it must stay closeable.
            if row.get('status') in _REMEDIATION_INFLIGHT:
                return {'error': f'Remediation #{rid} is leased by the executor and still running '
                        f"(status {row.get('status')!r}). Closing it now would strand that job. "
                        'Wait for it to finish, then close it.'}

            # The two console routes write DIFFERENT fields, and the drawer
            # reads them: resolutionHtml() paints "resolved by <who>" whenever
            # result.resolution_note is set, whatever the status. Writing the
            # resolve keys on a rejected row would label it resolved.
            note = note[:2000]  # parity with the console's cap
            if status == 'approved':
                # The console's Approve (POST /api/remediations/<id>/approve):
                # the same policy refuses manual-class rows and rows whose PR
                # is already open, then the row goes to the executor as
                # 'queued'. No note is stored — the route takes none, and the
                # executor's result overwrites the row.
                conflict = self.operator.kb.remediation_approve_conflict(row)
                if conflict:
                    return {'error': f'Remediation #{rid} cannot be approved: {conflict}'}
                ok = self.operator.kb.update_remediation_status(rid, 'queued')
            elif status == 'resolved':
                ok = self.operator.kb.update_remediation_status(
                    rid, 'resolved',
                    result={'resolved_by': 'chat-agent', 'resolution_note': note},
                )
            else:
                ok = self.operator.kb.update_remediation_status(
                    rid, 'rejected', last_error=note,
                )
            if not ok:
                return {'error': f'Remediation #{rid} could not be updated.'}
            # Re-read so the model reports the stored state, not its intent.
            return {'success': True,
                    'remediation': self._trim_remediation_for_tool(
                        self.operator.kb.get_remediation(rid) or {})}
        except Exception as e:
            return {'error': str(e)}

    def _is_morning_summary_report(self, report_id: int) -> bool:
        """True if this sweep report is a stored morning summary.

        Best-effort: a lookup failure must not block a legitimate finding
        update, so an error here reads as "not a summary".
        """
        try:
            report = self.operator.kb.get_sweep_report(int(report_id))
        except Exception:
            return False
        meta = (report or {}).get('sweep_meta')
        return isinstance(meta, dict) and meta.get('type') == 'morning_summary'

    @staticmethod
    def _trim_remediation_for_tool(row: Dict[str, Any]) -> Dict[str, Any]:
        """Shrink bulky remediation fields so tool output stays chat-sized."""
        out = dict(row or {})
        payload = dict(out.get('payload') or {}) if isinstance(out.get('payload'), dict) else {}
        for key in ('rendered_context', 'proposed_diff', 'evidence', 'recommendation'):
            val = payload.get(key)
            if isinstance(val, str) and len(val) > 2000:
                payload[key] = val[:2000] + '…'
        out['payload'] = payload
        result = out.get('result')
        if isinstance(result, dict):
            # Keep attribution keys; drop large blobs if any.
            out['result'] = {k: v for k, v in result.items()
                             if not (isinstance(v, str) and len(v) > 2000)}
        return out

    @staticmethod
    def _trim_investigation_for_tool(inv: Dict[str, Any]) -> Dict[str, Any]:
        """Shrink investigation findings for chat tool output."""
        out = dict(inv or {})
        findings = out.get('findings')
        if isinstance(findings, dict):
            trimmed = dict(findings)
            for key in ('response',):
                val = trimmed.get(key)
                if isinstance(val, str) and len(val) > 2000:
                    trimmed[key] = val[:2000] + '…'
            out['findings'] = trimmed
        elif isinstance(findings, str) and len(findings) > 2000:
            out['findings'] = findings[:2000] + '…'
        return out

    def _get_correlations(self, hours: int = 24) -> Dict[str, Any]:
        """Get event correlations and service failure patterns."""
        try:
            # get_correlation_summary is on the underlying KB, not the resilient wrapper
            kb = self.operator.kb
            if hasattr(kb, '_kb'):
                summary = kb._kb.get_correlation_summary(hours)
            else:
                summary = kb.get_correlation_summary(hours)
            return {'success': True, **summary}
        except Exception as e:
            return {'error': str(e)}

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

    def _make_timescale_tool_wrapper(self, tool_name: str):
        """Create wrapper function for TimescaleDB tools."""
        method_map = {
            'timescale_query': 'query',
        }

        method_name = method_map.get(tool_name)
        if not method_name:
            return lambda **kwargs: {'error': f'Unknown timescale tool: {tool_name}'}

        method = getattr(self.timescale_tools, method_name)

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

    def _make_k8s_tool_wrapper(self, tool_name: str):
        """Create wrapper function for K8s tools."""
        # Map tool names to K8sTools methods
        method_map = {
            'k8s_get_pods': 'get_pods',
            'k8s_get_pod_status': 'get_pod_status',
            'k8s_get_pod_logs': 'get_pod_logs',
            'k8s_get_deployments': 'get_deployments',
            'k8s_rollout_status': 'rollout_status',
            'k8s_rollout_restart': 'rollout_restart',
            'k8s_get_services': 'get_services',
            'k8s_get_ingresses': 'get_ingresses',
            'k8s_get_events': 'get_events',
            'k8s_describe': 'describe',
            'k8s_get_nodes': 'get_nodes',
            'k8s_get_node_metrics': 'get_node_metrics',
            'k8s_exec_pod': 'exec_pod',
            'k8s_get_namespaces': 'get_namespaces',
            'k8s_get_all_unhealthy': 'get_all_unhealthy',
            'k8s_get_cluster_info': 'get_cluster_info'
        }

        method_name = method_map.get(tool_name)
        if not method_name:
            return lambda **kwargs: {'error': f'Unknown K8s tool: {tool_name}'}

        def wrapper(**kwargs):
            try:
                if tool_name == 'k8s_exec_pod':
                    refusal = self._exec_pod_refusal(kwargs.get('namespace'), kwargs.get('pod_name'))
                    if refusal:
                        logger.warning(f"k8s_exec_pod refused: {refusal}")
                        return {'error': refusal, 'refused': True, 'tool': tool_name}
                # Resolved at call time so a replaced k8s_tools (tests, a
                # re-initialised client) is what runs, not the one bound at
                # registration.
                return getattr(self.k8s_tools, method_name)(**kwargs)
            except Exception as e:
                return {'error': str(e), 'tool': tool_name}

        return wrapper

    # ---- the agent's own datastore is off-limits to k8s_exec_pod (CFOP-124) --
    #
    # Session 21 (2026-08-28) resolved two queue rows by running psql inside
    # the knowledge-base pod through k8s_exec_pod — credential guessing, a
    # schema-probing SELECT, then an UPDATE the console never writes. The KB
    # has first-class tools for every legitimate write, so nothing in chat
    # needs a shell inside it, whoever is asking.
    #
    # Nothing here is deployment-specific: the protected set is DERIVED from
    # the connections the agent itself opens (the KB's db_url, the TimescaleDB
    # host), plus whatever chat.protected_exec_hosts adds. Another install with
    # its database under any name in any namespace is covered the same way.

    # A pod backs a service when its name is the service name itself, or the
    # service name plus a suffix of the shape a controller generates:
    #   StatefulSet        kb-db-0            -> ordinal
    #   Deployment/RS      kb-db-7f9c6b4d8-x2k9p -> replicaset hash + pod suffix
    # Bare startswith(service + '-') over-matches — with service `kb`, the
    # unrelated `kb-database-0` would be refused. DaemonSet pods (`name-xxxxx`)
    # are deliberately not matched: a datastore is not a DaemonSet, and that
    # shape collides with ordinary names like `kb-cache`.
    _POD_SUFFIX = re.compile(r'^(?:\d+|[a-z0-9]{5,10}-[a-z0-9]{5})$')

    def _namespace_exists(self, namespace: str) -> Optional[bool]:
        """Whether the cluster has this namespace. None if it cannot be asked.

        One kubectl call, cached for the process: the hosts this is asked about
        come from config and never change, so in practice it runs once. A
        failed lookup is not cached — it is not an answer.
        """
        cache = getattr(self, '_ns_cache', None)
        if cache is None:
            cache = self._ns_cache = {}
        if namespace in cache:
            return cache[namespace]
        try:
            result = self.k8s_tools.get_namespaces()
        except Exception as e:
            logger.debug(f"Namespace lookup failed: {e}")
            return None
        if not (isinstance(result, dict) and result.get('success')):
            return None
        names = {str(n.get('name') or '').lower() for n in result.get('namespaces') or []}
        if not names:
            return None
        for known in names:
            cache[known] = True
        cache.setdefault(namespace, namespace in names)
        return cache[namespace]

    def _protected_exec_targets(self) -> List[Tuple[str, Optional[str], str]]:
        """(service, namespace-or-None, what it is) for every datastore host.

        A bare service name is filled in with this agent's own namespace —
        what Kubernetes DNS would resolve it in — and left None when that is
        unknown, which protects the name in every namespace: a refusal with an
        explanation is the cheap mistake.

        A two-label host is ambiguous (``kb-db.aweoriujwoedf`` is cluster DNS,
        ``timescale.local`` is a LAN name) so the cluster settles it: if that
        namespace exists it is cluster DNS, and if it does not the host is
        somewhere k8s_exec_pod cannot reach and nothing needs protecting. When
        the cluster cannot be asked at all, the target is kept — an unnecessary
        refusal is recoverable, and a datastore left open is the hole this
        exists to close.
        """
        targets: List[Tuple[str, Optional[str], str]] = []

        def add(host, source):
            parsed = _service_from_host(host)
            if not parsed:
                return
            service, namespace, ambiguous = parsed
            if ambiguous:
                exists = self._namespace_exists(namespace)
                if exists is False:
                    logger.debug(f"{host} has no namespace {namespace!r} in this cluster; "
                                 "treating it as an external name")
                    return
            if namespace is None:
                namespace = _own_namespace()
            targets.append((service, namespace, source))

        try:
            db_url = getattr(getattr(self.operator, 'kb', None), 'db_url', None)
            if isinstance(db_url, str) and db_url:
                add(urlsplit(db_url).hostname, 'the knowledge base')
        except Exception as e:  # a KB wrapper that refuses attribute access
            logger.debug(f"Could not read the knowledge-base host: {e}")
        ts_host = getattr(self.timescale_tools, 'host', None)
        if isinstance(ts_host, str):
            add(ts_host, 'TimescaleDB')
        config = getattr(self.operator, 'config', None)
        chat_cfg = config.get('chat') if isinstance(config, dict) else None
        for extra in ((chat_cfg or {}).get('protected_exec_hosts') or []):
            if isinstance(extra, str):
                add(extra, 'chat.protected_exec_hosts')
        return targets

    def _pod_backs_service(self, pod: str, service: str) -> bool:
        if pod == service:
            return True
        if not pod.startswith(service + '-'):
            return False
        return bool(self._POD_SUFFIX.match(pod[len(service) + 1:]))

    def _exec_pod_refusal(self, namespace, pod_name) -> Optional[str]:
        """Why k8s_exec_pod must not run in this pod, or None.

        An omitted namespace is resolved the way kubectl would resolve it —
        the agent's own namespace — rather than being treated as a wildcard;
        with no namespace known at all it stays a wildcard, which is the
        fail-closed end of an unanswerable question.
        """
        ns = str(namespace or '').strip().lower() or (_own_namespace() or '')
        pod = str(pod_name or '').strip().lower()
        if not pod:
            return None
        for service, svc_ns, source in self._protected_exec_targets():
            if svc_ns is not None and ns and svc_ns != ns:
                continue
            if self._pod_backs_service(pod, service):
                where = f"{ns}/{pod}" if ns else pod
                return (f"k8s_exec_pod refuses {where}: it backs {source}, the agent's own "
                        "datastore. The knowledge base has its own tools — list_remediations, "
                        "resolve_remediation, store_learning, find_learnings — use those; nothing "
                        "in chat needs a shell inside it.")
        return None

    def _make_git_tool_wrapper(self, tool_name: str):
        """Create wrapper function for Git tools."""
        method_map = {
            'git_recent_commits': 'recent_commits',
            'git_diff_summary': 'diff_summary',
            'git_show_file': 'show_file',
            'git_blame': 'blame',
            'git_log_path': 'log_path',
        }
        method_name = method_map.get(tool_name)
        if not method_name:
            return lambda **kwargs: {'error': f'Unknown git tool: {tool_name}'}
        method = getattr(self.git_tools, method_name)

        def wrapper(**kwargs):
            try:
                return method(**kwargs)
            except Exception as e:
                return {'error': str(e), 'tool': tool_name}

        return wrapper

    def _make_github_tool_wrapper(self, tool_name: str):
        """Create wrapper function for GitHub API tools."""
        method_map = {
            'github_list_recent_prs': 'list_recent_prs',
            'github_get_pr': 'get_pr',
            'github_list_recent_commits': 'list_recent_commits',
            'github_get_issue': 'get_issue',
            'github_search_issues': 'search_issues',
            'github_get_file_contents': 'get_file_contents',
            'github_compare_commits': 'compare_commits',
            'github_create_pr': 'create_pr',
            'github_create_issue_comment': 'create_issue_comment',
        }
        method_name = method_map.get(tool_name)
        if not method_name:
            return lambda **kwargs: {'error': f'Unknown GitHub tool: {tool_name}'}
        method = getattr(self.github_tools, method_name)

        def wrapper(**kwargs):
            try:
                return method(**kwargs)
            except Exception as e:
                return {'error': str(e), 'tool': tool_name}

        return wrapper

__all__ = ['ToolRegistry']
