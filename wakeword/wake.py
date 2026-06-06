#!/usr/bin/env python3
"""
Hey Neo — offline wake word daemon.

Wake word (openWakeWord) → command recognition (vosk) → hub HTTP call.

Setup:
  1. Run install.sh — it trains the "Hey Neo" ONNX model and installs the service.
  2. sudo systemctl start wakeword
  No API keys required.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path

import numpy as np
import pyaudio
import requests
import vosk
from dotenv import load_dotenv
from openwakeword.model import Model

load_dotenv(Path(__file__).parent / "config.env")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OWW_MODEL      = os.getenv("OWW_MODEL", "hey_neo.onnx")
OWW_THRESHOLD  = float(os.getenv("OWW_THRESHOLD", "0.5"))
OWW_MODEL_PATH = Path(__file__).parent / OWW_MODEL
HUB_URL        = os.getenv("HUB_URL", "http://localhost:5001")
RECORD_SECONDS = int(os.getenv("RECORD_SECONDS", "4"))
VOSK_MODEL_PATH = Path(__file__).parent / "vosk-model-small-en-us"

SAMPLE_RATE = 16000
CHUNK       = 1280   # 80 ms — openWakeWord's native chunk size

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
    if not OWW_MODEL_PATH.exists():
        logging.error(f"Wake word model not found: {OWW_MODEL_PATH} — run install.sh to train it.")
        return

    if not VOSK_MODEL_PATH.exists():
        logging.error(f"Vosk model not found at {VOSK_MODEL_PATH} — run install.sh first.")
        return

    logging.info("Loading Vosk model...")
    vosk_model = vosk.Model(str(VOSK_MODEL_PATH))

    logging.info(f"Loading openWakeWord model: {OWW_MODEL_PATH.name}")
    oww = Model(wakeword_models=[str(OWW_MODEL_PATH)], inference_framework="onnx")
    model_key = OWW_MODEL_PATH.stem  # "hey_neo"

    pa = pyaudio.PyAudio()
    stream = pa.open(
        rate=SAMPLE_RATE,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=CHUNK,
    )

    logging.info(f"Listening for 'Hey Neo'... (hub: {HUB_URL}, threshold: {OWW_THRESHOLD})")

    try:
        while True:
            audio = np.frombuffer(
                stream.read(CHUNK, exception_on_overflow=False),
                dtype=np.int16,
            )
            scores = oww.predict(audio, debounce_time=3.0)

            if scores.get(model_key, 0.0) < OWW_THRESHOLD:
                continue

            logging.info(f"Wake word detected (score={scores[model_key]:.3f}) — listening for command...")
            threading.Thread(target=_wake_feedback, daemon=True).start()

            # Capture RECORD_SECONDS of audio
            n_chunks = int(SAMPLE_RATE / CHUNK * RECORD_SECONDS)  # 50 chunks @ 4 s
            frames = [stream.read(CHUNK, exception_on_overflow=False) for _ in range(n_chunks)]

            # Transcribe
            rec = vosk.KaldiRecognizer(vosk_model, SAMPLE_RATE)
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


if __name__ == "__main__":
    main()
