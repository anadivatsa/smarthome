#!/usr/bin/env python3
# SCHEDULE: daily at 07:30
# ENABLED: false
# DESCRIPTION: Send morning briefing to Telegram
"""Morning briefing: date, Bayern fixture, uptime, last 3 scenes, weather."""

import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

if not BOT_TOKEN or not CHAT_ID:
    raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")


def send(text: str) -> None:
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )


def uptime() -> str:
    try:
        r = subprocess.run(["uptime", "-p"], capture_output=True, text=True)
        return r.stdout.strip()
    except Exception:
        return "unknown"


def weather() -> str:
    try:
        with urllib.request.urlopen("https://wttr.in/Mumbai?format=3", timeout=8) as r:
            return r.read().decode().strip()
    except Exception:
        return "weather unavailable"


def bayern_today() -> str:
    try:
        from bavaria_notifier import get_matches, find_bayern_match
        match = find_bayern_match(get_matches())
        if not match:
            return "No Bayern fixture today"
        t1 = match["team1"]["teamName"]
        t2 = match["team2"]["teamName"]
        dt = match.get("matchDateTimeUTC", "")[:16].replace("T", " ")
        return f"⚽ {t1} vs {t2} at {dt} UTC"
    except Exception as exc:
        return f"Bayern check failed: {exc}"


def scene_history() -> str:
    try:
        import memory
        memory.init()
        log = memory.get_scene_history(n=3)
        if not log:
            return "No scenes yet"
        return "\n".join(f"• {e['scene']} via {e['triggered_by']}" for e in log)
    except Exception as exc:
        return f"Scene history unavailable: {exc}"


now = datetime.now(timezone.utc)
lines = [
    f"☀️ <b>Good morning!</b> {now.strftime('%A, %d %B %Y')}",
    "",
    f"🕰 Uptime: {uptime()}",
    f"🌤 {weather()}",
    f"🏟 {bayern_today()}",
    "",
    "<b>Last scenes:</b>",
    scene_history(),
]
send("\n".join(lines))
print("Morning brief sent.")
