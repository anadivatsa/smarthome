#!/usr/bin/env python3
"""
neo_mic.py — Neo mic node for Termux (Android)

Records audio via termux-microphone-record (no sounddevice/OpenSLES needed),
POSTs each chunk to Neo's /api/voice for Whisper→Claude dispatch.

── Setup (run once in Termux) ──────────────────────────────────────────────
  pkg install termux-api python
  pip install requests
  # Also install Termux:API app from F-Droid and grant mic permission

── Run in foreground ────────────────────────────────────────────────────────
  python neo_mic.py

── Run as persistent background process ────────────────────────────────────
  nohup python neo_mic.py >> ~/neo_mic.log 2>&1 &
  echo $! > ~/neo_mic.pid
  kill $(cat ~/neo_mic.pid)   # stop it

── Tune CHUNK_SEC ───────────────────────────────────────────────────────────
  Default 4s. Shorter = more responsive but more API calls.
  Longer = fewer calls but commands feel delayed.
"""

import logging
import os
import subprocess
import sys
import tempfile
import time

try:
    import requests
except ImportError:
    print("ERROR: requests not found. Run: pip install requests")
    sys.exit(1)

# ── Config ───────────────────────────────────────────────────────────────────
HUB_URL     = os.getenv("NEO_HUB_URL",  "http://192.168.1.8:5001")
API_KEY     = os.getenv("NEO_API_KEY",  "")
CHUNK_SEC   = int(os.getenv("CHUNK_SEC",   "4"))
SAMPLE_RATE = int(os.getenv("SAMPLE_RATE", "16000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("neo_mic")


# ── Audio capture ─────────────────────────────────────────────────────────────

def record_chunk(path: str) -> bool:
    """
    Record CHUNK_SEC seconds to path via termux-microphone-record.
    termux-microphone-record may return immediately (non-blocking), so we
    sleep for the duration then stop explicitly before reading the file.
    """
    subprocess.Popen(
        ["termux-microphone-record", "-f", path,
         "-l", str(CHUNK_SEC), "-r", str(SAMPLE_RATE), "-c", "1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(CHUNK_SEC + 1)
    subprocess.run(
        ["termux-microphone-record", "-q"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1)
    return os.path.exists(path) and os.path.getsize(path) > 0


# ── Hub POST ─────────────────────────────────────────────────────────────────

def post_audio(path: str) -> dict | None:
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"{HUB_URL}/api/voice",
                files={"audio": ("clip.wav", f, "audio/wav")},
                headers={"X-Neo-Key": API_KEY},
                timeout=60,   # first call loads Whisper on hub (~30s)
            )
        if r.ok:
            return r.json()
        log.warning("Hub %d: %s", r.status_code, r.text[:120])
    except Exception as exc:
        log.error("POST failed: %s", exc)
    return None


# ── Main loop ────────────────────────────────────────────────────────────────

def run():
    log.info("Neo mic node — hub: %s  chunk: %ds", HUB_URL, CHUNK_SEC)
    if not API_KEY:
        log.warning("NEO_API_KEY not set — hub will reject requests")

    while True:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        try:
            if not record_chunk(tmp.name):
                log.warning("Record produced no file — is Termux:API installed and mic permission granted?")
                time.sleep(2)
                continue

            result = post_audio(tmp.name)
            if result:
                t = result.get("transcript", "")
                if t:
                    log.info('Heard: "%s"', t)
                for act in result.get("actions", []):
                    if act.get("action"):
                        log.info("→ %s  (%s)", act["action"], act.get("reason", ""))
                    else:
                        log.debug("No-op: %s", act.get("reason", ""))
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("Stopped.")
