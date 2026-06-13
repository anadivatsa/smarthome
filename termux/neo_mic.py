#!/usr/bin/env python3
"""
neo_mic.py — Neo mic node for Termux (Android)

Captures phone mic continuously, detects speech via energy threshold,
POSTs WAV chunks to Neo hub /api/voice for Whisper→Claude dispatch.

── Setup (run once in Termux) ──────────────────────────────────────────────
  pkg install python libportaudio
  pip install sounddevice numpy requests
  export NEO_API_KEY="QbWBj9LS58rQSueXAAI6eWm2Xrx6gAIU_okG7-53n9c"
  export NEO_HUB_URL="http://192.168.1.8:5001"    # default, can omit

── Run in foreground ────────────────────────────────────────────────────────
  python neo_mic.py

── Run as persistent background process ────────────────────────────────────
  nohup python neo_mic.py >> ~/neo_mic.log 2>&1 &
  echo $! > ~/neo_mic.pid          # save PID to kill later
  kill $(cat ~/neo_mic.pid)        # stop it

── Tune ENERGY_THRESH ───────────────────────────────────────────────────────
  Default 500 works for most Android mics in a quiet room.
  If it triggers on silence → raise (600–800).
  If it misses quiet speech → lower (300–400).
  Run with --calibrate to measure your ambient noise floor.
"""

import io
import logging
import os
import sys
import time
import wave
from collections import deque

import numpy as np
import requests

try:
    import sounddevice as sd
except ImportError:
    print("ERROR: sounddevice not found.")
    print("Run:  pkg install libportaudio && pip install sounddevice numpy requests")
    sys.exit(1)

# ── Config ───────────────────────────────────────────────────────────────────
HUB_URL        = os.getenv("NEO_HUB_URL",    "http://192.168.1.8:5001")
API_KEY        = os.getenv("NEO_API_KEY",    "")
SAMPLE_RATE    = 16000
CHANNELS       = 1
DTYPE          = "int16"
CHUNK_MS       = 30
CHUNK_FRAMES   = SAMPLE_RATE * CHUNK_MS // 1000   # 480 samples per frame
ENERGY_THRESH  = int(os.getenv("ENERGY_THRESH",  "500"))
SILENCE_SEC    = float(os.getenv("SILENCE_SEC",  "1.0"))
MIN_SPEECH_SEC = float(os.getenv("MIN_SPEECH_SEC", "0.4"))
PRE_PAD_MS     = 300

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("neo_mic")

# ── Helpers ──────────────────────────────────────────────────────────────────

def rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))


def frames_to_wav(frames: list) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(f.tobytes() for f in frames))
    return buf.getvalue()


def post_audio(wav_bytes: bytes) -> dict | None:
    try:
        r = requests.post(
            f"{HUB_URL}/api/voice",
            files={"audio": ("clip.wav", wav_bytes, "audio/wav")},
            headers={"X-Neo-Key": API_KEY},
            timeout=60,   # first call loads Whisper on hub (~30s)
        )
        if r.ok:
            return r.json()
        log.warning("Hub %d: %s", r.status_code, r.text[:120])
    except Exception as exc:
        log.error("POST failed: %s", exc)
    return None


# ── Calibration mode ─────────────────────────────────────────────────────────

def calibrate(seconds: int = 5):
    """Measure ambient noise floor to help set ENERGY_THRESH."""
    print(f"\nCalibrating — stay quiet for {seconds} seconds…")
    samples = []
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                        dtype=DTYPE, blocksize=CHUNK_FRAMES) as stream:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            frame, _ = stream.read(CHUNK_FRAMES)
            samples.append(rms(frame))
    floor   = float(np.mean(samples))
    peak    = float(np.max(samples))
    suggest = int(floor * 3)
    print(f"\n  Ambient floor : {floor:.0f} RMS")
    print(f"  Ambient peak  : {peak:.0f} RMS")
    print(f"  Suggested ENERGY_THRESH: {suggest}")
    print(f"\n  Run: export ENERGY_THRESH={suggest}")
    print(f"  Then: python neo_mic.py\n")


# ── Main loop ────────────────────────────────────────────────────────────────

def run():
    pad_frames    = PRE_PAD_MS // CHUNK_MS
    ring          = deque(maxlen=pad_frames)
    voiced        = []
    in_speech     = False
    silence_count = 0
    silence_limit = round(SILENCE_SEC * 1000 / CHUNK_MS)

    log.info("Neo mic node — hub: %s  threshold: %d", HUB_URL, ENERGY_THRESH)
    if not API_KEY:
        log.warning("NEO_API_KEY not set — hub will reject requests")

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                        dtype=DTYPE, blocksize=CHUNK_FRAMES) as stream:
        log.info("Microphone open — listening…")
        while True:
            frame, _ = stream.read(CHUNK_FRAMES)
            energy   = rms(frame)
            is_voice = energy > ENERGY_THRESH

            if not in_speech:
                ring.append(frame)
                if is_voice:
                    in_speech     = True
                    silence_count = 0
                    voiced        = list(ring)
                    log.info("Speech start  energy=%.0f", energy)
            else:
                voiced.append(frame)
                if is_voice:
                    silence_count = 0
                else:
                    silence_count += 1
                    if silence_count >= silence_limit:
                        duration = len(voiced) * CHUNK_MS / 1000
                        in_speech     = False
                        silence_count = 0

                        if duration < MIN_SPEECH_SEC:
                            log.debug("Too short (%.2fs) — skipped", duration)
                            voiced = []
                            continue

                        log.info("Speech end — %.1fs — posting to hub…", duration)
                        wav    = frames_to_wav(voiced)
                        voiced = []

                        result = post_audio(wav)
                        if result:
                            t = result.get("transcript", "")
                            if t:
                                log.info('Heard: "%s"', t)
                            for act in result.get("actions", []):
                                if act.get("action"):
                                    log.info("→ %s  (%s)", act["action"], act.get("reason", ""))
                                else:
                                    log.info("No-op: %s", act.get("reason", ""))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--calibrate":
        calibrate()
        sys.exit(0)
    try:
        run()
    except KeyboardInterrupt:
        log.info("Stopped.")
