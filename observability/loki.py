"""Loki Logs Backend Implementation"""
from datetime import datetime
from typing import List, Dict, Any
import requests
from .base import LogsBackend

class LokiLogs(LogsBackend):
    """Loki implementation of LogsBackend."""

    def __init__(self, url: str = 'http://localhost:3100'):
        self.url = url.rstrip('/')
        self.timeout = 30

    def query(self, query: str, since: str = '1h', limit: int = 100) -> List[Dict[str, Any]]:
        """Query logs using LogQL."""
        params = {
            'query': query,
            'limit': limit,
            'start': f'now-{since}'
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
