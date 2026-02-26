"""Discord Notifications Backend Implementation"""
import logging
import os
import requests
from typing import Optional
from .base import NotificationBackend

logger = logging.getLogger(__name__)


class DiscordNotifications(NotificationBackend):
    """Discord implementation of NotificationBackend via webhooks."""

    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = webhook_url or os.getenv('DISCORD_WEBHOOK_URL')

    def send(self, message: str, severity: str = 'info') -> bool:
        """Send Discord notification."""
        if not self.webhook_url:
            return False

        color = {
            'info': 0x3498DB,       # blue
            'warning': 0xF39C12,    # orange
            'critical': 0xE74C3C,   # red
        }.get(severity, 0x95A5A6)   # grey

        # Discord embeds for richer formatting
        payload = {
            'embeds': [{
                'title': f'CFOperator — {severity.upper()}',
                'description': message[:4096],  # Discord embed limit
                'color': color,
            }]
        }

        resp = requests.post(self.webhook_url, json=payload, timeout=10)
        return resp.status_code in (200, 204)
