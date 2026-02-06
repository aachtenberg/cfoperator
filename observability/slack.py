"""Slack Notifications Backend Implementation"""
import os
import requests
from typing import Optional
from .base import NotificationBackend

class SlackNotifications(NotificationBackend):
    """Slack implementation of NotificationBackend."""

    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = webhook_url or os.getenv('SLACK_WEBHOOK_URL')

    def send(self, message: str, severity: str = 'info') -> bool:
        """Send Slack notification."""
        if not self.webhook_url:
            return False

        emoji = {
            'info': ':information_source:',
            'warning': ':warning:',
            'critical': ':rotating_light:'
        }.get(severity, ':robot_face:')

        payload = {
            'text': f'{emoji} *CFOperator*\n{message}'
        }

        resp = requests.post(self.webhook_url, json=payload, timeout=10)
        return resp.status_code == 200
