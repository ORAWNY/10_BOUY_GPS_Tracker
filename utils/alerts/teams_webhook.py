# utils/alerts/teams_webhook.py
from __future__ import annotations
import os
import requests
from datetime import datetime


def send_teams_text(text: str, webhook_url: str | None = None) -> None:
    """
    Send a simple text message to a Microsoft Teams channel via Incoming Webhook.

    Params
    ------
    text : str
        Message body to post in Teams.
    webhook_url : str | None
        Incoming webhook URL. If omitted, TEAMS_WEBHOOK_URL env var is used.
    """
    webhook_url = webhook_url or os.environ.get("TEAMS_WEBHOOK_URL")
    if not webhook_url:
        raise ValueError("No webhook_url provided and TEAMS_WEBHOOK_URL not set")

    payload = {"text": text}

    resp = requests.post(webhook_url, json=payload, timeout=10)
    try:
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"Teams webhook post failed: {resp.status_code} {resp.text}") from e
