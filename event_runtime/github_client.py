"""Shared GitHub API client utilities.

Used by both the tool layer and the event runtime so authentication,
timeouts, retries, caching, and slug validation stay consistent.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger("cfoperator.github_client")

DEFAULT_GITHUB_API_URL = "https://api.github.com"
DEFAULT_GITHUB_API_VERSION = "2022-11-28"
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
REPO_SEGMENT_RE = r"[a-zA-Z0-9](?:[a-zA-Z0-9._-]{0,98}[a-zA-Z0-9])?"
REPO_SLUG_RE = re.compile(rf"^{REPO_SEGMENT_RE}/{REPO_SEGMENT_RE}$")


def validate_repo_slug(slug: str) -> str:
    """Validate and return an ``owner/repo`` slug."""
    if not REPO_SLUG_RE.fullmatch(slug):
        raise ValueError(f"Invalid repo slug: {slug}")
    return slug


class GitHubApiClient:
    """Small stdlib-only GitHub REST client with retries and TTL caching."""

    def __init__(
        self,
        token: Optional[str] = None,
        api_url: str = DEFAULT_GITHUB_API_URL,
        *,
        timeout: int = 30,
        max_retries: int = 2,
        backoff_base_seconds: float = 0.5,
    ):
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._cache: Dict[tuple[str, str, tuple[tuple[str, str], ...]], tuple[float, Dict[str, Any]]] = {}

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[dict] = None,
        params: Optional[Dict[str, str]] = None,
        cache_ttl: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Make a GitHub API request, optionally authenticated."""
        normalized_method = method.upper()
        normalized_params = {k: str(v) for k, v in (params or {}).items()}
        cache_key = self._cache_key(normalized_method, path, normalized_params) if cache_ttl and normalized_method == "GET" else None

        if cache_key is not None:
            cached = self._get_cached_response(cache_key)
            if cached is not None:
                return cached

        url = f"{self._api_url}{path}"
        if normalized_params:
            url = f"{url}?{urllib.parse.urlencode(normalized_params)}"

        data = json.dumps(body).encode() if body else None

        for attempt in range(self._max_retries + 1):
            req = urllib.request.Request(url, data=data, method=normalized_method)
            if self._token:
                req.add_header("Authorization", f"Bearer {self._token}")
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("X-GitHub-Api-Version", DEFAULT_GITHUB_API_VERSION)
            if data:
                req.add_header("Content-Type", "application/json")

            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    raw_body = resp.read()
                    parsed_body = json.loads(raw_body) if raw_body else None
                    headers = self._normalize_headers(getattr(resp, "headers", None))
                    remaining = headers.get("X-RateLimit-Remaining")
                    if remaining is not None and int(remaining) < 50:
                        logger.warning("GitHub API rate limit low: %s remaining", remaining)
                    response = {"success": True, "data": parsed_body, "status": resp.status, "headers": headers}
                    if cache_key is not None:
                        self._cache[cache_key] = (time.monotonic() + float(cache_ttl), copy.deepcopy(response))
                    return response
            except urllib.error.HTTPError as exc:
                error_body = exc.read().decode(errors="replace") if exc.fp else ""
                headers = self._normalize_headers(exc.headers)
                if exc.code in RETRYABLE_STATUS_CODES and attempt < self._max_retries:
                    delay = self._retry_delay_seconds(headers, attempt)
                    logger.warning(
                        "GitHub API request failed with %s for %s; retrying in %.2fs (%d/%d)",
                        exc.code,
                        path,
                        delay,
                        attempt + 1,
                        self._max_retries,
                    )
                    time.sleep(delay)
                    continue
                return {"success": False, "status": exc.code, "error": error_body, "headers": headers}
            except urllib.error.URLError as exc:
                return {"success": False, "error": str(exc.reason or exc)}
            except Exception as exc:
                return {"success": False, "error": str(exc)}

        return {"success": False, "error": f"GitHub API request failed after {self._max_retries + 1} attempts"}

    def get_paginated(
        self,
        path: str,
        *,
        params: Optional[Dict[str, str]] = None,
        per_page: int = 100,
        max_pages: int = 10,
        cache_ttl: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Fetch a paginated list endpoint until exhausted or ``max_pages`` is reached."""
        all_items: list[Any] = []
        base_params = {k: str(v) for k, v in (params or {}).items()}

        for page in range(1, max_pages + 1):
            page_params = dict(base_params)
            page_params["per_page"] = str(per_page)
            page_params["page"] = str(page)
            response = self.request("GET", path, params=page_params, cache_ttl=cache_ttl)
            if not response["success"]:
                return response
            items = response.get("data")
            if not isinstance(items, list):
                return {
                    "success": False,
                    "status": response.get("status"),
                    "error": f"Expected list response for paginated endpoint: {path}",
                }
            all_items.extend(items)
            if len(items) < per_page:
                break

        return {"success": True, "data": all_items, "status": 200}

    def _cache_key(self, method: str, path: str, params: Dict[str, str]) -> tuple[str, str, tuple[tuple[str, str], ...]]:
        return (method, path, tuple(sorted(params.items())))

    def _get_cached_response(self, cache_key: tuple[str, str, tuple[tuple[str, str], ...]]) -> Optional[Dict[str, Any]]:
        entry = self._cache.get(cache_key)
        if not entry:
            return None
        expires_at, response = entry
        if time.monotonic() >= expires_at:
            self._cache.pop(cache_key, None)
            return None
        return copy.deepcopy(response)

    def _retry_delay_seconds(self, headers: Dict[str, str], attempt: int) -> float:
        retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return min(5.0, self._backoff_base_seconds * (2**attempt))

    def _normalize_headers(self, headers: Any) -> Dict[str, str]:
        if headers is None:
            return {}
        if hasattr(headers, "items"):
            return {str(key): str(value) for key, value in headers.items()}
        try:
            return {str(key): str(value) for key, value in headers}
        except TypeError:
            return {}