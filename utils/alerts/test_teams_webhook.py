# utils/alerts/test_teams_webhook.py
from __future__ import annotations
from datetime import datetime

from utils.alerts.teams_webhook import send_teams_text

# 🔧 Paste your actual Teams / Power Automate webhook URL here:
WEBHOOK_URL = "https://default63629420161f48ccaefcea8301e027.2d.environment.api.powerplatform.com:443/powerautomate/automations/direct/workflows/135a1d2f153043379274b39df85a8e33/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=rFwgpOoJO1rXKX7ji25Y8YHun_SNdwn4Dz3PSQTFyu8"


def main() -> None:
    # Change this message to whatever you like
    msg = f"{datetime.now():%Y-%m-%d %H:%M:%S} — Test message from Bouy stale-alert setup"
    send_teams_text(msg, WEBHOOK_URL)
    print("✅ Sent to Teams:", msg)


if __name__ == "__main__":
    main()
