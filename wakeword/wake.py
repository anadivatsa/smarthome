#!/usr/bin/env python3
"""
Hey Neo — offline wake word daemon.

Wake word (pvporcupine) → command recognition (vosk) → hub HTTP call.

Setup:
  1. Get a free Porcupine access key at console.picovoice.ai
  2. Optionally train a custom "Hey Neo" wake word there and download the .ppn
  3. Add both to config.env
  4. Run install.sh to set up venv + vosk model + systemd service
"""

import json
import logging
import os
import struct
import threading
import time
from pathlib import Path

import pvporcupine
import pyaudio
import requests
import vosk
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / "config.env")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORCUPINE_KEY   = os.environ.get("PORCUPINE_KEY", "")
WAKE_WORD_MODEL = os.getenv("WAKE_WORD_MODEL", "")
HUB_URL         = os.getenv("HUB_URL", "http://localhost:5001")
RECORD_SECONDS  = int(os.getenv("RECORD_SECONDS", "4"))
VOSK_MODEL_PATH = Path(__file__).parent / "vosk-model-small-en-us"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)

# ---------------------------------------------------------------------------
# Command map  (spoken phrase → hub endpoint)
# ---------------------------------------------------------------------------

COMMANDS = {
    # Scenes
    "movie time":        "/scene/movie",
    "movie":             "/scene/movie",
    "netflix":           "/scene/netflix",
    "party time":        "/scene/party",
    "party":             "/scene/party",
    "play music":        "/scene/music",
    "music":             "/scene/music",
    "good night":        "/scene/goodnight",
    "goodnight":         "/scene/goodnight",
    "focus mode":        "/scene/focus",
    "focus":             "/scene/focus",
    "morning":           "/scene/morning",
    "dinner time":       "/scene/dinner",
    "dinner":            "/scene/dinner",
    "gaming mode":       "/scene/gaming",
    "gaming":            "/scene/gaming",
    "romance mode":      "/scene/romance",
    "romance":           "/scene/romance",
    "turn off":          "/scene/off",
    "lights off":        "/scene/off",
    "thunderstruck":     "/scene/thunderstruck",
    # Spotify
    "next song":         "/spotify/next",
    "next":              "/spotify/next",
    "skip":              "/spotify/next",
    "previous song":     "/spotify/prev",
    "go back":           "/spotify/prev",
    "pause":             "/spotify/pause",
    "pause music":       "/spotify/pause",
    "play":              "/spotify/play",
    "resume":            "/spotify/play",
    "resume music":      "/spotify/play",
    "shuffle on":        "/spotify/shuffle/on",
    "shuffle off":       "/spotify/shuffle/off",
    "beat sync on":      "/spotify/beat-sync/on",
    "beat sync off":     "/spotify/beat-sync/off",
    # Lamp
    "lights on":         "/lamp/on",
    "disco":             "/lamp/disco",
    "disco mode":        "/lamp/disco",
    "party lights":      "/lamp/party",
    "relax light":       "/lamp/relax",
    "bright":            "/lamp/focus",
    "aurora":            "/lamp/aurora",
    # TV
    "tv on":             "/tv/on",
    "tv off":            "/tv/off",
    "mute":              "/tv/mute",
    "volume up":         "/tv/volume/5",
    "volume down":       "/tv/volume/-5",
}

# Sorted longest-first so "movie time" matches before "movie"
_SORTED_COMMANDS = sorted(COMMANDS.items(), key=lambda kv: len(kv[0]), reverse=True)


def _match(text: str) -> str | None:
    text = text.lower().strip()
    for phrase, endpoint in _SORTED_COMMANDS:
        if phrase in text:
            return endpoint
    return None


# ---------------------------------------------------------------------------
# Hub call
# ---------------------------------------------------------------------------

def _call_hub(endpoint: str):
    try:
        r = requests.get(f"{HUB_URL}{endpoint}", timeout=10)
        logging.info(f"→ {endpoint}  [{r.status_code}]")
    except Exception as exc:
        logging.error(f"Hub call failed: {exc}")


# ---------------------------------------------------------------------------
# Wake feedback — brief lamp pulse without disrupting current state
# ---------------------------------------------------------------------------

def _wake_feedback():
    try:
        status = requests.get(f"{HUB_URL}/lamp/status", timeout=2).json()
        prev_brightness = status.get("brightness_pct") or 40
        requests.get(f"{HUB_URL}/lamp/brightness/100", timeout=2)
        time.sleep(0.35)
        requests.get(f"{HUB_URL}/lamp/brightness/{prev_brightness}", timeout=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not PORCUPINE_KEY:
        logging.error("PORCUPINE_KEY not set in config.env — exiting.")
        return

    if not VOSK_MODEL_PATH.exists():
        logging.error(f"Vosk model not found at {VOSK_MODEL_PATH} — run install.sh first.")
        return

    # Load Vosk model (slow — do once at startup)
    logging.info("Loading Vosk model...")
    vosk_model = vosk.Model(str(VOSK_MODEL_PATH))

    # Init Porcupine
    ppn = Path(WAKE_WORD_MODEL) if WAKE_WORD_MODEL else None
    if ppn and ppn.exists():
        porcupine = pvporcupine.create(
            access_key=PORCUPINE_KEY,
            keyword_paths=[str(ppn)],
        )
        logging.info(f"Wake word: custom model {ppn.name}")
    else:
        porcupine = pvporcupine.create(
            access_key=PORCUPINE_KEY,
            keywords=["hey google"],
        )
        logging.warning("No .ppn found — using built-in 'hey google' as fallback. "
                        "Train a custom 'Hey Neo' at console.picovoice.ai")

    pa = pyaudio.PyAudio()
    stream = pa.open(
        rate=porcupine.sample_rate,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=porcupine.frame_length,
    )

    logging.info(f"Listening... (hub: {HUB_URL})")

    try:
        while True:
            raw = stream.read(porcupine.frame_length, exception_on_overflow=False)
            pcm = struct.unpack_from(f"{porcupine.frame_length}h", raw)

            if porcupine.process(pcm) < 0:
                continue

            logging.info("Wake word detected — listening for command...")
            threading.Thread(target=_wake_feedback, daemon=True).start()

            # Capture RECORD_SECONDS of audio
            n_chunks = int(porcupine.sample_rate / porcupine.frame_length * RECORD_SECONDS)
            frames = [stream.read(porcupine.frame_length, exception_on_overflow=False)
                      for _ in range(n_chunks)]

            # Transcribe
            rec = vosk.KaldiRecognizer(vosk_model, porcupine.sample_rate)
            rec.AcceptWaveform(b"".join(frames))
            text = json.loads(rec.FinalResult()).get("text", "").strip()
            logging.info(f"Heard: '{text}'")

            endpoint = _match(text)
            if endpoint:
                _call_hub(endpoint)
            else:
                logging.info("No command matched.")

    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()
        porcupine.delete()


if __name__ == "__main__":
    main()
