"""Read-only client for the CFOperator agent API — the data source for `attach`.

`cfassist attach <investigation-id>` runs from the operator's laptop, not from
inside the cluster, so everything it needs comes over the same HTTP API the
console and the MCP facade use. Auth is the console's database-backed bearer
token (``auth_api_tokens``, minted at ``/admin?tab=tokens``); nothing new is
introduced here. The environment variable names match ``mcp_server/client.py``
so a workstation already configured for the MCP server needs no extra setup.

**Read-only is enforced, not merely intended.** ``_request`` refuses any method
outside ``_ALLOWED_METHODS``, which contains only GET. Approving, rejecting or
queueing a remediation is a console/MCP action; an attached session must not be
able to reach for it even by accident, because the whole premise of handing an
incident to a terminal agent is that the handoff itself changes nothing.
"""

import os

import httpx

DEFAULT_AGENT_URL = "http://127.0.0.1:8083"

# Same names mcp_server/client.py reads. CFOP_API_TOKEN is no longer a single
# shared secret (retired 2026-08-09, see docs/auth.md) — it is the variable each
# consumer mounts *its own* token as, which is exactly what cfassist is doing.
ENV_AGENT_URL = "CFOP_AGENT_URL"
ENV_API_TOKEN = "CFOP_API_TOKEN"


class CFOperatorError(Exception):
    """A CFOperator API call could not be completed.

    Carries an optional operator-facing ``hint``: the CLI prints it under the
    error, because most failures here are configuration (no token, wrong URL,
    agent not port-forwarded) rather than genuine faults.
    """

    def __init__(self, message, *, hint=None):
        super().__init__(message)
        self.message = message
        self.hint = hint


class CFOperatorClient:
    """Thin synchronous, read-only HTTP client for the agent API."""

    # The read-only guard. Not a convention — the transport checks it, so a
    # later contributor adding a `POST /approve` helper gets an exception in
    # their first test run rather than a surprising mutation in production.
    _ALLOWED_METHODS = frozenset({"GET"})

    def __init__(self, url=None, token="", timeout=30.0, transport=None):
        self.url = (url or DEFAULT_AGENT_URL).rstrip("/")
        self.token = (token or "").strip()
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._http = httpx.Client(
            base_url=self.url,
            timeout=timeout,
            headers=headers,
            transport=transport,
        )

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    # --- transport ---------------------------------------------------------

    def _request(self, method, path, params=None):
        if method not in self._ALLOWED_METHODS:
            raise CFOperatorError(
                f"cfassist attach is read-only; refusing {method} {path}",
                hint="Use the console or the MCP server to act on a remediation.",
            )

        try:
            resp = self._http.request(method, path, params=params)
        except httpx.ConnectError:
            raise CFOperatorError(
                f"Cannot reach CFOperator at {self.url}",
                hint="Is the agent up and reachable? "
                     "kubectl -n apps port-forward svc/cfoperator 8083:8083",
            )
        except httpx.TimeoutException:
            raise CFOperatorError(f"Timed out talking to CFOperator at {self.url}")
        except httpx.HTTPError as exc:
            raise CFOperatorError(f"CFOperator request failed: {exc}")

        if resp.status_code in (401, 403):
            raise CFOperatorError(
                f"CFOperator rejected the API token (HTTP {resp.status_code})",
                hint=f"Mint one at {self.url}/admin?tab=tokens and export "
                     f"{ENV_API_TOKEN}, or set cfoperator.token in ~/.cfassist/config.yaml.",
            )
        if resp.status_code == 404:
            raise CFOperatorError(f"Not found: {path}")
        if resp.status_code >= 400:
            raise CFOperatorError(
                f"CFOperator returned HTTP {resp.status_code} for {path}: "
                f"{resp.text[:200]}"
            )

        try:
            return resp.json()
        except ValueError:
            raise CFOperatorError(
                f"CFOperator returned a non-JSON response for {path}"
            )

    def _get(self, path, params=None):
        return self._request("GET", path, params=params)

    # --- reads -------------------------------------------------------------

    def get_investigation(self, investigation_id):
        """Full investigation detail.

        Note the API's shape: the list endpoint returns ``outcome`` at the top
        level but no findings at all, while this one nests ``provider``,
        ``response`` and ``recommendation`` under ``findings``. Callers that
        read the top level only see an empty report — see briefing.py.
        """
        return self._get(f"/api/investigations/{int(investigation_id)}")

    def list_remediations(self, status=None, limit=200):
        params = {"limit": limit}
        if status:
            params["status"] = status
        payload = self._get("/api/remediations", params=params) or {}
        return payload.get("remediations") or []

    def remediations_for_investigation(self, investigation_id, limit=200):
        """Queue rows linked to one investigation.

        Filtered client-side: ``/api/remediations`` has no ``investigation_id``
        parameter. Adding one would be a server change in service of a client
        convenience, and the queue is small enough that one list call is
        cheaper than a new endpoint to maintain.
        """
        wanted = int(investigation_id)
        return [
            row for row in self.list_remediations(limit=limit)
            if row.get("investigation_id") == wanted
        ]

    def search_knowledge(self, query, limit=5):
        """Hybrid (or FTS-fallback) KB search. Returns ``(rows, mode)``.

        The two modes return *different row shapes* — the hybrid SQL path omits
        ``investigation_id`` and adds similarity scores, the FTS fallback does
        the reverse — so the mode is returned alongside the rows and the
        formatter tolerates both.
        """
        payload = self._get(
            "/api/kb/search", params={"q": query, "limit": limit}
        ) or {}
        return (payload.get("results") or []), str(payload.get("mode") or "")

    # --- composed read -----------------------------------------------------

    def collect_attach_context(self, investigation_id, *, learning_limit=5,
                               remediation_limit=200):
        """Gather everything `attach` seeds a session with.

        The investigation itself is required — without it there is no briefing
        and the command should fail loudly. The remediation rows and the KB
        learnings are *enrichment*: if either lookup fails the briefing is
        still worth having, so the failure is recorded as a warning that the
        briefing prints rather than an exception that costs the operator the
        session. Degrading visibly beats failing totally when someone is
        mid-incident.
        """
        investigation = self.get_investigation(investigation_id)
        if not isinstance(investigation, dict) or not investigation.get("id"):
            raise CFOperatorError(
                f"CFOperator returned no investigation #{investigation_id}"
            )

        warnings = []

        try:
            remediations = self.remediations_for_investigation(
                investigation["id"], limit=remediation_limit
            )
        except CFOperatorError as exc:
            remediations, warnings = [], warnings + [
                f"remediation queue unavailable: {exc.message}"
            ]

        learnings, learnings_mode = [], ""
        trigger = str(investigation.get("trigger") or "").strip()
        if trigger:
            try:
                learnings, learnings_mode = self.search_knowledge(
                    trigger[:400], limit=learning_limit
                )
            except CFOperatorError as exc:
                warnings.append(f"knowledge search unavailable: {exc.message}")

        return {
            "investigation": investigation,
            "remediations": remediations,
            "learnings": learnings,
            "learnings_mode": learnings_mode,
            "warnings": warnings,
            "console_url": self.url,
        }


def resolve_endpoint(section=None, env=None):
    """Resolve (url, token, timeout) for the CFOperator API.

    Config file wins over the environment: a config value of ``${CFOP_API_TOKEN}``
    has already been expanded by ``config._expand_env_vars`` at this point, so a
    file that opts into the variable still reads it, while a file that hardcodes
    a different endpoint is not silently overridden by a stale export.
    """
    section = section or {}
    env = os.environ if env is None else env

    url = str(section.get("url") or "").strip()
    if not url:
        url = str(env.get(ENV_AGENT_URL) or "").strip() or DEFAULT_AGENT_URL

    token = str(section.get("token") or "").strip()
    if not token:
        token = str(env.get(ENV_API_TOKEN) or "").strip()

    try:
        timeout = float(section.get("timeout") or 30)
    except (TypeError, ValueError):
        timeout = 30.0

    return url.rstrip("/"), token, timeout
