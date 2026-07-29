"""Environment-driven settings for the CFOperator MCP server."""

import os
from dataclasses import dataclass

# Scope hierarchy: each scope implies the ones below it.
SCOPE_IMPLIES = {
    "read": frozenset({"read"}),
    "investigate": frozenset({"read", "investigate"}),
    "remediate": frozenset({"read", "investigate", "remediate"}),
}

_TRANSPORT_ALIASES = {"http": "streamable-http"}
VALID_TRANSPORTS = ("stdio", "streamable-http")


def expand_scopes(raw):
    """Expand a comma-split scope list into its implied closure."""
    scopes = set()
    for item in raw:
        name = item.strip().lower()
        if not name:
            continue
        if name not in SCOPE_IMPLIES:
            raise ValueError(
                f"unknown scope {name!r}; valid scopes: {sorted(SCOPE_IMPLIES)}")
        scopes |= SCOPE_IMPLIES[name]
    if not scopes:
        raise ValueError("CFOP_MCP_SCOPES must grant at least one scope")
    return frozenset(scopes)


@dataclass(frozen=True)
class Settings:
    agent_url: str = "http://127.0.0.1:8083"
    event_runtime_url: str | None = None
    token: str | None = None
    scopes: frozenset = frozenset({"read"})
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8090
    request_timeout: float = 30.0
    chat_timeout: float = 300.0
    chat_poll_interval: float = 2.0
    skills_dir: str = "./skills"

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env

        transport = (env.get("CFOP_MCP_TRANSPORT") or "stdio").strip().lower()
        transport = _TRANSPORT_ALIASES.get(transport, transport)
        if transport not in VALID_TRANSPORTS:
            raise ValueError(
                f"CFOP_MCP_TRANSPORT={transport!r} invalid; use one of {VALID_TRANSPORTS}")

        token = env.get("CFOP_MCP_TOKEN") or None
        # A network listener without a bearer token would hand every LAN
        # client the full scope set — refuse to start instead.
        if transport == "streamable-http" and not token:
            raise ValueError(
                "CFOP_MCP_TOKEN is required for the streamable-http transport; "
                "refusing to start an unauthenticated network listener")

        return cls(
            agent_url=(env.get("CFOP_AGENT_URL") or cls.agent_url).rstrip("/"),
            event_runtime_url=(env.get("CFOP_EVENT_RUNTIME_URL") or None),
            token=token,
            scopes=expand_scopes((env.get("CFOP_MCP_SCOPES") or "read").split(",")),
            transport=transport,
            host=env.get("CFOP_MCP_HOST") or cls.host,
            port=int(env.get("CFOP_MCP_PORT") or cls.port),
            request_timeout=float(env.get("CFOP_MCP_REQUEST_TIMEOUT") or cls.request_timeout),
            chat_timeout=float(env.get("CFOP_MCP_CHAT_TIMEOUT") or cls.chat_timeout),
            chat_poll_interval=float(
                env.get("CFOP_MCP_CHAT_POLL_INTERVAL") or cls.chat_poll_interval),
            skills_dir=env.get("CFOP_SKILLS_DIR") or cls.skills_dir,
        )
