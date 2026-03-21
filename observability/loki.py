"""Loki Logs Backend Implementation"""
import re
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Tuple
import requests
from .base import LogsBackend

# Parse duration strings like "1h", "30m", "2d", "1h30m"
_DURATION_RE = re.compile(r'(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?')


def _parse_duration(since: str) -> timedelta:
    """Parse a duration string (e.g. '1h', '30m', '2d') into a timedelta."""
    m = _DURATION_RE.fullmatch(since.strip())
    if not m or not any(m.groups()):
        return timedelta(hours=1)  # default fallback
    days = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    minutes = int(m.group(3) or 0)
    seconds = int(m.group(4) or 0)
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def _fix_unterminated_strings(query: str) -> str:
    """Fix unterminated string literals in filter pipeline (e.g. |= "error -> |= "error")."""
    # Find the end of the stream selector
    brace_depth = 0
    selector_end = -1
    for i, ch in enumerate(query):
        if ch == '{':
            brace_depth += 1
        elif ch == '}':
            brace_depth -= 1
            if brace_depth == 0:
                selector_end = i
                break
    if selector_end == -1:
        return query

    pipeline = query[selector_end + 1:]
    if not pipeline:
        return query

    # Count quotes in the pipeline portion — odd means unterminated
    if pipeline.count('"') % 2 != 0:
        pipeline = pipeline.rstrip()
        # Append closing quote if the pipeline ends with an unterminated string
        if not pipeline.endswith('"'):
            pipeline += '"'
        query = query[:selector_end + 1] + pipeline

    return query


def validate_logql(query: str) -> Tuple[bool, str]:
    """
    Validate a LogQL query for common syntax errors.
    Returns (is_valid, error_message).
    """
    query = query.strip()

    # Must start with a stream selector {
    if not query.startswith('{'):
        return False, f"Query must start with a stream selector. Got: {query[:50]}"

    # Find the closing brace of the stream selector
    brace_depth = 0
    selector_end = -1
    for i, ch in enumerate(query):
        if ch == '{':
            brace_depth += 1
        elif ch == '}':
            brace_depth -= 1
            if brace_depth == 0:
                selector_end = i
                break

    if selector_end == -1:
        return False, "Unclosed stream selector - missing closing '}'"

    selector = query[1:selector_end]

    # Empty selector is usually a mistake
    if not selector.strip():
        return False, 'Empty stream selector {}. Specify at least one label, e.g. {host="raspberrypi2"}'

    # Check for 'and' or 'or' between stream selectors (common LLM mistake)
    remainder = query[selector_end + 1:]
    if re.search(r'\b(and|or)\b\s*\{', remainder, re.IGNORECASE):
        return False, 'Cannot use and/or between stream selectors. Combine labels in one selector: {job="docker", container_name="foo"}'

    # Check for quoted stream selector (common LLM mistake)
    if query.startswith('{"') or query.startswith("{\'" ):
        return False, 'Stream selector should not be quoted. Use {container_name="foo"} not {"container_name=..."}'

    # Check for glob patterns instead of regex
    if '*' in selector and '.*' not in selector and '=~' in selector:
        return False, 'Use regex syntax (.*) not glob (*) in label matchers. Example: {container_name=~"immich.*"}'

    # Loki requires at least one positive matcher (=, =~) that isn't empty-compatible.
    # Negative-only matchers (!=, !~) cause: "queries require at least one regexp or
    # equality matcher that does not have an empty-compatible value"
    matchers = re.findall(r'(\w+)\s*(=~|=|!=|!~)\s*"([^"]*)"', selector)
    has_positive = any(
        op in ('=', '=~') and val and val not in ('.*', '.+', '')
        for _, op, val in matchers
    )
    if not has_positive:
        return False, (
            'Loki requires at least one positive label matcher (= or =~). '
            'Negative-only selectors like {host!="x"} are rejected. '
            'Use a positive matcher first, e.g. {job="docker", host!="x"} or {namespace="apps"} |~ "error"'
        )

    return True, ''


class LokiLogs(LogsBackend):
    """Loki implementation of LogsBackend."""

    def __init__(self, url: str = 'http://localhost:3100'):
        self.url = url.rstrip('/')
        self.timeout = 30

    def query(self, query: str, since: str = '1h', limit: int = 100) -> List[Dict[str, Any]]:
        """Query logs using LogQL with input validation and auto-repair."""
        query = _fix_unterminated_strings(query)
        is_valid, error_msg = validate_logql(query)
        if not is_valid:
            raise ValueError(f"Invalid LogQL query: {error_msg}")

        now = datetime.now(timezone.utc)
        start = now - _parse_duration(since)
        params = {
            'query': query,
            'limit': limit,
            'start': int(start.timestamp()),
            'end': int(now.timestamp()),
        }
        resp = requests.get(
            f'{self.url}/loki/api/v1/query_range', params=params, timeout=self.timeout
        )
        if resp.status_code == 400:
            body = resp.text[:300]
            raise ValueError(f"Loki returned 400 Bad Request for query: {query}. Response: {body}")
        resp.raise_for_status()
        return resp.json().get('data', {}).get('result', [])

    def query_range(self, query: str, start: datetime, end: datetime, limit: int = 1000) -> List[Dict[str, Any]]:
        """Query logs over time range."""
        query = _fix_unterminated_strings(query)
        is_valid, error_msg = validate_logql(query)
        if not is_valid:
            raise ValueError(f"Invalid LogQL query: {error_msg}")

        params = {
            'query': query,
            'limit': limit,
            'start': int(start.timestamp() * 1e9),
            'end': int(end.timestamp() * 1e9),
        }
        resp = requests.get(
            f'{self.url}/loki/api/v1/query_range', params=params, timeout=self.timeout
        )
        if resp.status_code == 400:
            body = resp.text[:300]
            raise ValueError(f"Loki returned 400 Bad Request for query: {query}. Response: {body}")
        resp.raise_for_status()
        return resp.json().get('data', {}).get('result', [])
