#!/usr/bin/env python3
"""
bt_presence.py — Bluetooth presence detection daemon for Neo.

Polls the paired iPhone's Bluetooth MAC every POLL_INTERVAL seconds using
l2ping. Triggers hub scenes and updates presence state on arrival/departure.

State machine:
  unknown  — startup; transitions recorded but no scene triggered on first "away"
  home     — phone confirmed in range
  away     — phone confirmed out of range (after AWAY_THRESHOLD misses)

Transitions:
  * → home  : POST /presence home  +  trigger BT_HOME_SCENE (default: relax)
  home → away: trigger BT_AWAY_SCENE (default: leave, which sets presence=away)
  unknown → away: POST /presence away, no scene (avoid spurious triggers on boot)

Environment (load from hub.env + bt_presence.env):
  PHONE_MAC         Required. iPhone Bluetooth MAC, e.g. 5C:AD:BA:AC:67:98
  NEO_API_KEY       Required. Hub API key.
  HUB_URL           Default: http://localhost:5001
  BT_HOME_SCENE     Default: relax
  BT_AWAY_SCENE     Default: leave
  POLL_INTERVAL     Default: 30  (seconds between checks)
  HOME_THRESHOLD    Default: 2   (consecutive hits to confirm arrival — filters BLE blips)
  AWAY_THRESHOLD    Default: 6   (consecutive misses before declaring away)
  L2PING_TIMEOUT    Default: 5   (seconds per l2ping attempt)
"""

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from datetime import datetime

# Load bt_presence.env if present
_ENV_FILE = Path(__file__).parent / "bt_presence.env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# Also load hub.env for NEO_API_KEY
_HUB_ENV = Path(__file__).parent / "hub.env"
if _HUB_ENV.exists():
    for _line in _HUB_ENV.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# Config
PHONE_MAC       = os.getenv("PHONE_MAC", "").strip()
HUB_URL         = os.getenv("HUB_URL", "http://localhost:5001")
NEO_API_KEY     = os.getenv("NEO_API_KEY", "").strip()
BT_HOME_SCENE   = os.getenv("BT_HOME_SCENE", "relax")
BT_AWAY_SCENE   = os.getenv("BT_AWAY_SCENE", "leave")
POLL_INTERVAL   = int(os.getenv("POLL_INTERVAL", "30"))
HOME_THRESHOLD  = int(os.getenv("HOME_THRESHOLD", "2"))
AWAY_THRESHOLD  = int(os.getenv("AWAY_THRESHOLD", "6"))
L2PING_TIMEOUT  = int(os.getenv("L2PING_TIMEOUT", "5"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [bt_presence] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bt_presence")


def _hub(path: str, method: str = "GET", body: dict | None = None) -> bool:
    """Call hub API. Returns True on success."""
    import urllib.request, json  # noqa: PLC0415
    url = f"{HUB_URL}{path}"
    if NEO_API_KEY:
        url += ("&" if "?" in url else "?") + f"key={NEO_API_KEY}"
    try:
        data = json.dumps(body).encode() if body else None
        headers = {"Content-Type": "application/json"} if body else {}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status < 300
    except Exception as exc:
        log.warning("hub call failed %s: %s", path, exc)
        return False


def _hub_bg(path: str, method: str = "GET", body: dict | None = None) -> None:
    """Fire a hub call in a background thread so the poll loop never blocks."""
    threading.Thread(target=_hub, args=(path, method, body), daemon=True).start()


def _in_range(mac: str) -> bool:
    """Return True if the device responds to a single l2ping."""
    try:
        result = subprocess.run(
            ["l2ping", "-c", "1", "-t", str(L2PING_TIMEOUT), mac],
            capture_output=True, timeout=L2PING_TIMEOUT + 2,
        )
        return result.returncode == 0
    except Exception:
        return False


def run() -> None:
    if not PHONE_MAC:
        log.error("PHONE_MAC not set — set it in bt_presence.env and restart")
        sys.exit(1)

    log.info("Starting — phone MAC: %s", PHONE_MAC)
    log.info("Poll every %ds, home after %d hits, away after %d misses, home→%s, away→%s",
             POLL_INTERVAL, HOME_THRESHOLD, AWAY_THRESHOLD, BT_HOME_SCENE, BT_AWAY_SCENE)

    state      = "unknown"  # "home" | "away" | "unknown"
    miss_count = 0
    hit_count  = 0

    while True:
        reachable = _in_range(PHONE_MAC)

        if reachable:
            miss_count = 0
            hit_count += 1
            if state != "home":
                if hit_count >= HOME_THRESHOLD:
                    prev = state
                    state = "home"
                    log.info("📱 Phone confirmed home (%d/%d hits) — %s → home",
                             hit_count, HOME_THRESHOLD, prev)
                    _hub_bg("/presence", method="POST", body={"state": "home"})
                    if prev != "unknown":
                        log.info("→ triggering scene/%s (arrival)", BT_HOME_SCENE)
                        _hub_bg(f"/scene/{BT_HOME_SCENE}")
                    else:
                        log.info("→ startup detection, presence set to home (no scene)")
                else:
                    log.debug("Hit %d/%d for %s (waiting for confirmation)",
                              hit_count, HOME_THRESHOLD, PHONE_MAC)
        else:
            hit_count = 0
            miss_count += 1
            log.debug("Miss %d/%d for %s", miss_count, AWAY_THRESHOLD, PHONE_MAC)

            if miss_count >= AWAY_THRESHOLD and state != "away":
                prev = state
                state = "away"
                log.info("🚶 Phone gone (%d misses) — %s → away", miss_count, prev)
                if prev == "home":
                    # Known departure: trigger full leave scene (sets presence=away)
                    log.info("→ triggering scene/%s (departure)", BT_AWAY_SCENE)
                    _hub_bg(f"/scene/{BT_AWAY_SCENE}")
                else:
                    # Boot with phone already away: just set presence, no scene
                    log.info("→ startup: phone not found, setting presence=away quietly")
                    _hub_bg("/presence", method="POST", body={"state": "away"})

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
