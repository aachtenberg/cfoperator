"""Loki Logs Backend Implementation"""
import re
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any
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


class LokiLogs(LogsBackend):
    """Loki implementation of LogsBackend."""

    def __init__(self, url: str = 'http://localhost:3100'):
        self.url = url.rstrip('/')
        self.timeout = 30

    def query(self, query: str, since: str = '1h', limit: int = 100) -> List[Dict[str, Any]]:
        """Query logs using LogQL."""
        now = datetime.now(timezone.utc)
        start = now - _parse_duration(since)
        params = {
            'query': query,
            'limit': limit,
            'start': int(start.timestamp()),
            'end': int(now.timestamp())
        }
        resp = requests.get(f'{self.url}/loki/api/v1/query_range', params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json().get('data', {}).get('result', [])

    def query_range(self, query: str, start: datetime, end: datetime, limit: int = 1000) -> List[Dict[str, Any]]:
        """Query logs over time range."""
        params = {
            'query': query,
            'limit': limit,
            'start': int(start.timestamp() * 1e9),
            'end': int(end.timestamp() * 1e9)
        }
        resp = requests.get(f'{self.url}/loki/api/v1/query_range', params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json().get('data', {}).get('result', [])
