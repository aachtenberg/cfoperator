"""MCP connectors: tools from external MCP servers an admin registers (CFOP-443).

The console chat could only use tools compiled into this registry. An admin
who wanted it to work in Plane, or anything else that already speaks MCP,
needed a code change. A connector is a URL plus request headers; its tools
join the registry as ``mcp_<connector>_<tool>`` and the chat sees them like
any other tool.

Decisions worth keeping:

**Streamable HTTP only.** That is MCP's remote transport, and the one
Plane's server serves in ``http`` mode (``/http/api-key/mcp`` with
``x-api-key`` and ``x-workspace-slug``). A stdio server would need a process
inside the agent pod for every connector; out of scope.

**One short session per call.** Listing and each call open a session,
initialize, do the one thing and close. A long-lived connection would need
reconnect logic, and a chat turn makes a handful of calls at most.

**Write unless the server says read.** A tool is ``mutating`` unless it
declares ``readOnlyHint: true``. So a member or a verification turn is
offered only the read-only ones, and an admin's call to a write tool is
logged with its arguments like every other mutating tool.

**Chat only.** Every connector tool carries ``attended_only``: it is
offered, and runs, only on a turn with a person behind it. Investigations,
sweeps and triage never see one; third-party output steering an unattended
run is a door nobody asked to open.

**Headers are sealed.** They carry credentials, so they are stored
encrypted (cfshared/sealed.py) and never come back out of the API. Their
names do, so an admin can see what is set.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from cfshared import sealed

logger = logging.getLogger(__name__)

SETTING_KEY = 'mcp_connectors'
HKDF_INFO = b'cfoperator mcp_connectors v1'
#: Short and plain: it becomes part of every tool name, and tool names are
#: capped at 64 characters by the strictest provider.
NAME_RE = re.compile(r'^[a-z][a-z0-9]{0,15}$')
#: Tool names every provider accepts.
TOOL_NAME_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
#: RFC 7230 token characters, which is what a header name may contain.
HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$")
MAX_CONNECTORS = 10
MAX_HEADERS = 20
MAX_HEADER_VALUE = 4096
MAX_URL = 500
MAX_TOOL_FILTER = 100
#: Seconds. Listing is on an admin's click or a background refresh; a call is
#: inside a chat turn whose whole budget is 180 s.
LIST_TIMEOUT = 15
CALL_TIMEOUT = 60
#: Characters of tool output handed back. The tool loop frames and clips again;
#: this only keeps a runaway response from being held in memory whole.
MAX_RESULT_CHARS = 20000
MAX_DESCRIPTION = 1500


class ConnectorError(ValueError):
    """A request the console should answer with 400, or a connector that
    could not be reached; the message is safe to show an admin."""


# ── Stored shape ─────────────────────────────────────────────────────────


def loads(raw: Any) -> List[Dict[str, Any]]:
    """The stored list, with anything malformed dropped. Never raises: a bad
    row must not take the other connectors down with it."""
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning(f"[MCP] the {SETTING_KEY} setting is not JSON; no connectors loaded")
        return []
    out, seen = [], set()
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        name = item.get('name')
        if not isinstance(name, str) or not NAME_RE.match(name) or name in seen:
            continue
        seen.add(name)
        out.append({
            'name': name,
            'url': str(item.get('url') or ''),
            'enabled': bool(item.get('enabled', True)),
            'tools': [t for t in (item.get('tools') or []) if isinstance(t, str)],
            'headers': str(item.get('headers') or ''),
            'header_names': [h for h in (item.get('header_names') or []) if isinstance(h, str)],
        })
    return out


def dumps(connectors: List[Dict[str, Any]]) -> str:
    return json.dumps(connectors, separators=(',', ':'))


def _parse_url(value: Any) -> str:
    url = str(value or '').strip()
    if not url:
        raise ConnectorError('url is required')
    if len(url) > MAX_URL:
        raise ConnectorError('url is too long')
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise ConnectorError('url must be an http:// or https:// address of an MCP server '
                             '(streamable HTTP transport)')
    if parts.username or parts.password:
        raise ConnectorError('put credentials in a header, not in the url; '
                             'the url is shown to every console user')
    return url


def _parse_tool_filter(value: Any) -> List[str]:
    if value is None or value == '':
        return []
    items = value.split(',') if isinstance(value, str) else value
    if not isinstance(items, list):
        raise ConnectorError('tools must be a list of tool names or a comma-separated string')
    names = []
    for item in items:
        name = str(item or '').strip()
        if not name:
            continue
        if not re.match(r'^[A-Za-z0-9_.-]{1,64}$', name):
            raise ConnectorError(f'not a tool name: {name!r}')
        if name not in names:
            names.append(name)
    if len(names) > MAX_TOOL_FILTER:
        raise ConnectorError(f'at most {MAX_TOOL_FILTER} tools in the filter')
    return names


def parse_headers(value: Any) -> Dict[str, str]:
    """Headers from the console: a JSON object, or ``Name: value`` lines."""
    if isinstance(value, dict):
        pairs = [(str(k), '' if v is None else str(v)) for k, v in value.items()]
    elif isinstance(value, str):
        pairs = []
        for n, line in enumerate(value.splitlines(), 1):
            if not line.strip():
                continue
            if ':' not in line:
                raise ConnectorError(f'header line {n} is not "Name: value"')
            k, v = line.split(':', 1)
            pairs.append((k, v))
    else:
        raise ConnectorError('headers must be an object or "Name: value" lines')
    headers: Dict[str, str] = {}
    for k, v in pairs:
        k, v = k.strip(), v.strip()
        if not HEADER_NAME_RE.match(k):
            raise ConnectorError(f'not a header name: {k!r}')
        if '\r' in v or '\n' in v:
            raise ConnectorError(f'header {k} has a line break in its value')
        if len(v) > MAX_HEADER_VALUE:
            raise ConnectorError(f'header {k} is too long')
        if k.lower() in {h.lower() for h in headers}:
            raise ConnectorError(f'header {k} is given twice')
        headers[k] = v
    if len(headers) > MAX_HEADERS:
        raise ConnectorError(f'at most {MAX_HEADERS} headers')
    return headers


def parse_input(name: str, body: Dict[str, Any],
                current: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The entry to store for a PUT. Fields the body omits keep their stored
    value, so a form that cannot show the header values cannot drop them.
    ``headers`` present and empty clears them. Raises ConnectorError on a bad
    request and sealed.SealUnavailable when headers cannot be stored."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ConnectorError('name must be 1-16 lowercase letters or digits, starting with a letter')
    if not isinstance(body, dict):
        raise ConnectorError('body must be a JSON object')
    entry = dict(current or {'name': name, 'url': '', 'enabled': True, 'tools': [],
                             'headers': '', 'header_names': []})
    entry['name'] = name
    if 'url' in body or current is None:
        entry['url'] = _parse_url(body.get('url'))
    if 'enabled' in body:
        entry['enabled'] = bool(body.get('enabled'))
    if 'tools' in body:
        entry['tools'] = _parse_tool_filter(body.get('tools'))
    if 'headers' in body:
        headers = parse_headers(body.get('headers') if body.get('headers') is not None else {})
        if headers:
            entry['headers'] = sealed.seal(json.dumps(headers), HKDF_INFO)
            entry['header_names'] = list(headers)
        else:
            entry['headers'], entry['header_names'] = '', []
    return entry


def upsert(connectors: List[Dict[str, Any]], entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = [c for c in connectors if c.get('name') != entry['name']]
    if len(out) >= MAX_CONNECTORS:
        raise ConnectorError(f'at most {MAX_CONNECTORS} connectors')
    position = next((i for i, c in enumerate(connectors) if c.get('name') == entry['name']), None)
    if position is None:
        out.append(entry)
    else:
        out.insert(position, entry)
    return out


def remove(connectors: List[Dict[str, Any]], name: str) -> Tuple[List[Dict[str, Any]], bool]:
    out = [c for c in connectors if c.get('name') != name]
    return out, len(out) != len(connectors)


def headers_for(entry: Dict[str, Any]) -> Tuple[Dict[str, str], Optional[str]]:
    """The connector's headers, or ({}, why) when they cannot be opened."""
    stored = entry.get('headers') or ''
    if not stored:
        return {}, None
    plain = sealed.unseal(stored, HKDF_INFO)
    if plain is None:
        return {}, (f'the stored headers cannot be decrypted ({sealed.SECRET_ENV} unset or '
                    'rotated); save them again')
    try:
        data = json.loads(plain)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {}, 'the stored headers are corrupt; save them again'
    return {str(k): str(v) for k, v in data.items()}, None


def public_view(entry: Dict[str, Any], status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What GET shows: never a header value."""
    stored = entry.get('headers') or ''
    if not stored:
        headers_state = 'none'
    else:
        headers_state = 'sealed' if sealed.unseal(stored, HKDF_INFO) is not None else 'unreadable'
    return {
        'name': entry.get('name'),
        'url': entry.get('url'),
        'enabled': bool(entry.get('enabled')),
        'tools': list(entry.get('tools') or []),
        'header_names': list(entry.get('header_names') or []),
        'headers_state': headers_state,
        'status': status or {},
    }


# ── Schemas and results ──────────────────────────────────────────────────

_SCALARS = {'string', 'number', 'integer', 'boolean', 'array', 'object'}


def _pick_type(schema: Dict[str, Any]) -> Optional[str]:
    t = schema.get('type')
    if isinstance(t, list):
        t = next((x for x in t if x != 'null'), None)
    if t in _SCALARS:
        return t
    if isinstance(schema.get('properties'), dict):
        return 'object'
    if isinstance(schema.get('items'), dict):
        return 'array'
    return None


def clean_schema(schema: Any, depth: int = 0) -> Dict[str, Any]:
    """The subset of JSON Schema every provider accepts.

    Servers emit what their framework generates: ``anyOf`` with ``null`` for
    optionals, ``default``, ``title``, ``additionalProperties``, ``$defs``.
    The stricter OpenAI-compatible endpoints 400 on some of those, so a
    connector is reduced to type, description, properties, required, items
    and enum. A nullable union becomes its non-null branch.
    """
    if not isinstance(schema, dict) or depth > 8:
        return {'type': 'string'}
    for key in ('anyOf', 'oneOf'):
        branches = schema.get(key)
        if isinstance(branches, list):
            real = [b for b in branches if isinstance(b, dict) and b.get('type') != 'null']
            if real:
                merged = dict(real[0])
                if schema.get('description') and not merged.get('description'):
                    merged['description'] = schema['description']
                return clean_schema(merged, depth + 1)
    out: Dict[str, Any] = {}
    t = _pick_type(schema)
    out['type'] = t or 'string'
    desc = schema.get('description')
    if isinstance(desc, str) and desc.strip():
        out['description'] = desc.strip()[:MAX_DESCRIPTION]
    enum = schema.get('enum')
    if isinstance(enum, list) and enum and all(isinstance(e, (str, int, float)) for e in enum):
        out['enum'] = enum
    if out['type'] == 'object':
        props = schema.get('properties') if isinstance(schema.get('properties'), dict) else {}
        out['properties'] = {str(k): clean_schema(v, depth + 1) for k, v in props.items()}
        required = schema.get('required')
        if isinstance(required, list):
            req = [r for r in required if isinstance(r, str) and r in out['properties']]
            if req:
                out['required'] = req
    elif out['type'] == 'array':
        out['items'] = clean_schema(schema.get('items') or {'type': 'string'}, depth + 1)
    return out


def registry_name(connector: str, tool: str) -> Optional[str]:
    """``mcp_<connector>_<tool>``, or None when it cannot be a tool name."""
    name = f"mcp_{connector}_{re.sub(r'[^A-Za-z0-9_-]', '_', tool)}"
    return name if TOOL_NAME_RE.match(name) else None


def result_to_dict(connector: str, result: Any) -> Dict[str, Any]:
    """A CallToolResult as the registry's dict shape."""
    parts: List[str] = []
    for item in getattr(result, 'content', None) or []:
        kind = getattr(item, 'type', None)
        if kind == 'text':
            parts.append(str(getattr(item, 'text', '') or ''))
        elif kind == 'resource':
            res = getattr(item, 'resource', None)
            text = getattr(res, 'text', None)
            parts.append(str(text) if text else f"[resource {getattr(res, 'uri', '')}]")
        else:
            parts.append(f'[{kind} content omitted]')
    text = '\n'.join(p for p in parts if p)
    structured = getattr(result, 'structuredContent', None)
    if not text and structured is not None:
        text = json.dumps(structured, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f'\n...[{len(text) - MAX_RESULT_CHARS} chars cut by the connector client]'
    if getattr(result, 'isError', False):
        return {'error': text or 'the connector reported an error and gave no detail',
                'connector': connector}
    return {'success': True, 'connector': connector, 'content': text}


# ── The client ───────────────────────────────────────────────────────────


def _leaf(exc: BaseException) -> BaseException:
    """The first real exception inside anyio's exception groups."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def describe_error(exc: BaseException) -> str:
    """A connection failure in words an admin can act on, never a header."""
    exc = _leaf(exc)
    response = getattr(exc, 'response', None)
    status = getattr(response, 'status_code', None)
    if status is not None:
        hint = ' (check the auth headers)' if status in (401, 403) else ''
        return f'the server answered HTTP {status}{hint}'
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return 'timed out'
    name = type(exc).__name__
    text = str(exc).strip()
    return f'{name}: {text}'[:300] if text else name


def _run(make_coro: Callable[[], Any], timeout: float) -> Any:
    """Run a coroutine on its own loop in a worker thread, with a deadline.

    Its own thread so a caller that already runs a loop is never affected,
    and so nothing outlives the call: the loop, the HTTP client and the
    session all belong to this one run.
    """
    box: Dict[str, Any] = {}

    def target():
        try:
            box['value'] = asyncio.run(asyncio.wait_for(make_coro(), timeout))
        except BaseException as e:  # noqa: BLE001 - handed to the caller below
            box['error'] = e

    worker = threading.Thread(target=target, name='mcp-connector', daemon=True)
    worker.start()
    worker.join(timeout + 5)
    if worker.is_alive():
        raise ConnectorError('timed out')
    if 'error' in box:
        raise ConnectorError(describe_error(box['error']))
    return box.get('value')


async def _with_session(url: str, headers: Dict[str, str], use):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    # Connect fast, read as long as a call may take. Redirects followed, as
    # the mcp library's own client does.
    client = httpx.AsyncClient(headers=headers or None, follow_redirects=True,
                               timeout=httpx.Timeout(CALL_TIMEOUT, connect=LIST_TIMEOUT))
    async with client:
        async with streamable_http_client(url, http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await use(session)


def list_remote_tools(url: str, headers: Dict[str, str]) -> List[Any]:
    """Every tool the server lists (paginated), as mcp ``Tool`` objects."""
    async def use(session):
        tools, cursor = [], None
        for _ in range(20):
            page = await session.list_tools(cursor)
            tools.extend(page.tools or [])
            cursor = getattr(page, 'nextCursor', None)
            if not cursor:
                break
        return tools
    return _run(lambda: _with_session(url, headers, use), LIST_TIMEOUT)


def call_remote_tool(url: str, headers: Dict[str, str], tool: str,
                     arguments: Dict[str, Any]) -> Any:
    async def use(session):
        return await session.call_tool(tool, arguments or {},
                                       read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT))
    return _run(lambda: _with_session(url, headers, use), CALL_TIMEOUT)


# ── Registry entries ─────────────────────────────────────────────────────


def _caller(entry: Dict[str, Any], remote: str) -> Callable[..., Dict[str, Any]]:
    """The registry function for one remote tool. Headers are opened per
    call, so a key saved after the last refresh is the one used."""
    snapshot = dict(entry)
    connector = snapshot['name']

    def call(**arguments):
        headers, why = headers_for(snapshot)
        if why:
            return {'error': f'connector {connector}: {why}', 'connector': connector}
        try:
            result = call_remote_tool(snapshot['url'], headers, remote, arguments)
        except ConnectorError as e:
            return {'error': f'connector {connector} could not run {remote}: {e}',
                    'connector': connector}
        return result_to_dict(connector, result)
    return call


def build_entries(connectors: List[Dict[str, Any]], reserved: set,
                  lister: Callable[[str, Dict[str, str]], List[Any]] = None
                  ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Registry entries for every enabled connector, and a status per connector.

    A connector that cannot be reached contributes no tools and says why; it
    never stops the others. ``reserved`` is the native tool names, which a
    connector tool may not shadow.
    """
    lister = lister or list_remote_tools
    entries: Dict[str, Dict[str, Any]] = {}
    status: Dict[str, Dict[str, Any]] = {}
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for entry in connectors:
        name = entry['name']
        if not entry.get('enabled'):
            status[name] = {'state': 'disabled', 'tools': [], 'checked_at': now}
            continue
        headers, why = headers_for(entry)
        if why:
            status[name] = {'state': 'error', 'error': why, 'tools': [], 'checked_at': now}
            continue
        try:
            remote_tools = lister(entry['url'], headers)
        except ConnectorError as e:
            logger.warning(f"[MCP] connector {name} could not list its tools: {e}")
            status[name] = {'state': 'error', 'error': str(e), 'tools': [], 'checked_at': now}
            continue
        wanted = set(entry.get('tools') or [])
        offered, read_only, skipped = [], [], []
        remote_names = set()
        for tool in remote_tools:
            remote = str(getattr(tool, 'name', '') or '')
            remote_names.add(remote)
            if not remote or (wanted and remote not in wanted):
                continue
            reg_name = registry_name(name, remote)
            if reg_name is None or reg_name in reserved or reg_name in entries:
                skipped.append(remote)
                continue
            annotations = getattr(tool, 'annotations', None)
            is_read_only = bool(getattr(annotations, 'readOnlyHint', False))
            desc = ' '.join(str(getattr(tool, 'description', '') or '').split())
            schema = {
                'name': reg_name,
                'description': (f'[{name} connector, MCP] {desc}' if desc
                                else f'[{name} connector, MCP] {remote}')[:MAX_DESCRIPTION],
                'parameters': clean_schema(getattr(tool, 'inputSchema', None) or {'type': 'object'}),
                'attended_only': True,
            }
            if schema['parameters'].get('type') != 'object':
                schema['parameters'] = {'type': 'object', 'properties': {}}
            if not is_read_only:
                schema['mutating'] = True
            else:
                read_only.append(reg_name)
            entries[reg_name] = {'function': _caller(entry, remote), 'schema': schema}
            offered.append(reg_name)
        missing = sorted(wanted - remote_names)
        status[name] = {'state': 'ok', 'tools': offered, 'read_only': read_only,
                        'remote_count': len(remote_names), 'skipped': skipped,
                        'missing': missing, 'checked_at': now}
        logger.info(f"[MCP] connector {name}: {len(offered)} tools offered "
                    f"({len(read_only)} read-only) of {len(remote_names)} listed")
    return entries, status
