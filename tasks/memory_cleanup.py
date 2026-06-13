#!/usr/bin/env python3
# SCHEDULE: weekly on sunday at 03:00
# ENABLED: false
# DESCRIPTION: Prune old conversation history from memory.db
"""Delete conversation rows older than 30 days and notify via Telegram."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import requests
import memory

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")


def send(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print(text)
        return
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
        timeout=10,
    )


memory.init()
pruned = memory.prune_conversation(days=30)
send(f"🧹 Memory cleanup done. {pruned} conversation rows pruned (>30 days old).")
print(f"Pruned {pruned} rows.")
