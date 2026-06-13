#!/usr/bin/env python3
# SCHEDULE: daily at 09:00
# ENABLED: true
# DESCRIPTION: Warn if Pi disk usage exceeds 85%
"""Alert via Telegram if root partition exceeds 85% usage."""

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import requests

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
THRESHOLD = 85


def send(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print(text)
        return
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )


result = subprocess.run(["df", "-h", "/"], capture_output=True, text=True)
for line in result.stdout.splitlines()[1:]:
    parts = line.split()
    if len(parts) >= 5:
        pct = int(parts[4].rstrip("%"))
        if pct >= THRESHOLD:
            send(
                f"⚠️ <b>Disk warning on Neo</b>\n"
                f"Root partition is <b>{pct}%</b> full "
                f"({parts[2]} used of {parts[1]})\n"
                f"Free: {parts[3]}"
            )
            print(f"Alert sent: {pct}% used")
        else:
            print(f"Disk OK: {pct}% used (threshold {THRESHOLD}%)")
        break
