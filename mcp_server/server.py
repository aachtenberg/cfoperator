"""FastMCP app assembly and transport selection."""

import logging

from mcp.server.fastmcp import FastMCP

from auth.bootstrap import init_auth_store
from mcp_server.auth import BearerAuthMiddleware
from mcp_server.client import CfopClient
from mcp_server.prompts import playbooks
from mcp_server.resources import digests as digest_resources
from mcp_server.resources import investigations as investigation_resources
from mcp_server.resources import remediations as remediation_resources
from mcp_server.tools import chat, investigate, knowledge, remediations, triage

logger = logging.getLogger(__name__)

INSTRUCTIONS = """CFOperator: SRE agent for a homelab k3s fleet.

Read tools (list/get investigations and remediations) are cheap. triage_alert
and ask_sre run an LLM server-side and can take minutes. start_investigation
is async — enqueue, then poll. approve_remediation hands work to an executor
that opens PRs; treat it as a mutating action and confirm intent first.
Failures come back as {"error": {code, message, retryable}}."""


def build_server(settings, client=None):
    server = FastMCP("cfoperator", instructions=INSTRUCTIONS,
                     host=settings.host, port=settings.port)
    client = client or CfopClient(settings)
    for module in (triage, investigate, chat, knowledge, remediations):
        module.register(server, client, settings)
    investigation_resources.register(server, client, settings)
    remediation_resources.register(server, client, settings)
    digest_resources.register(server, client, settings)
    playbooks.register(server, client, settings)
    return server


def run(settings):
    server = build_server(settings)
    if settings.transport == "stdio":
        logger.info("cfoperator-mcp on stdio (agent: %s, scopes: %s)",
                    settings.agent_url, sorted(settings.scopes))
        server.run("stdio")
        return

    # streamable-http: wrap the Starlette app so the bearer check runs before
    # any MCP session handling. Settings.from_env guarantees token is set.
    #
    # The store lets each caller present its own token and carry its own
    # scopes; without one, every client shares CFOP_MCP_TOKEN and the
    # process-wide CFOP_MCP_SCOPES grant. It is seeded elsewhere — this process
    # only verifies credentials, it does not create accounts.
    app = server.streamable_http_app()
    store = init_auth_store(seed_admin=False)
    if store is None:
        logger.warning(
            "no auth database — falling back to the shared CFOP_MCP_TOKEN, which "
            "grants every client the same %s scopes", sorted(settings.scopes))
    app.add_middleware(BearerAuthMiddleware, token=settings.token, store=store)
    logger.info("cfoperator-mcp on http://%s:%d/mcp (agent: %s, scopes: %s)",
                settings.host, settings.port, settings.agent_url,
                sorted(settings.scopes))
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
