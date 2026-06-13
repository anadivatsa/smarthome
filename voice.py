#!/usr/bin/env python3
"""
Voice Intelligence Layer — Smart Home Hub

Pipeline:
  mic (3.5mm) → WebRTC VAD → buffer utterance → Whisper (local)
  → Claude API intent → hub endpoint(s) on localhost:5001

System prompt is built at startup from scenes.json + known hub routes.
No hardcoded commands — Claude handles all intent recognition.
"""

import json
import logging
import os
import sys
import tempfile
import threading
import wave
from collections import deque
from pathlib import Path

import anthropic
import pyaudio
import requests
import webrtcvad
import whisper

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HUB_URL       = os.getenv("HUB_URL",       "http://localhost:5001")
MODEL         = os.getenv("CLAUDE_MODEL",  "claude-sonnet-4-20250514")
VAD_MODE      = int(os.getenv("VAD_MODE",  "2"))   # 0–3, 3 = most aggressive
INPUT_DEVICE  = os.getenv("INPUT_DEVICE", "")     # PyAudio device index; empty = system default
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")

SAMPLE_RATE   = 16000
CHANNELS      = 1
FRAME_MS      = 30                                  # webrtcvad supports 10 / 20 / 30 ms
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000     # 480 samples per frame
FRAME_BYTES   = FRAME_SAMPLES * 2                  # int16 = 2 bytes/sample

SILENCE_TIMEOUT = float(os.getenv("SILENCE_TIMEOUT", "0.9"))  # s of silence to end utterance
MIN_SPEECH_SEC  = float(os.getenv("MIN_SPEECH_SEC",  "0.4"))   # ignore sub-threshold blips
PADDING_MS      = 300                               # pre-speech padding kept in ring buffer

SCENES_FILE = Path(__file__).parent / "scenes.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [voice] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("voice")

# Serialise Whisper calls — only one transcription at a time to avoid OOM on Pi
_whisper_lock = threading.Semaphore(1)


# ---------------------------------------------------------------------------
# System prompt — built at startup from live hub config
# ---------------------------------------------------------------------------

def _scene_summary(name: str, cfg: dict) -> str:
    lamp = cfg.get("lamp", "—")
    tv   = cfg.get("tv", {})
    parts = [f"lamp={lamp}", f"tv={tv.get('action', '—')}"]
    if tv.get("app"):
        parts.append(f"app={tv['app']}")
    if cfg.get("spotify"):
        parts.append("spotify=play")
    if cfg.get("presence"):
        parts.append(f"presence={cfg['presence']}")
    return ", ".join(parts)


def build_system_prompt() -> str:
    scenes = json.loads(SCENES_FILE.read_text())
    scene_lines = "\n".join(
        f"  /scene/{name}  ({_scene_summary(name, cfg)})"
        for name, cfg in scenes.items()
    )

    return f"""You are the voice controller for a smart home hub (Raspberry Pi).
Your ONLY job: parse the voice transcript and return JSON action(s) to execute.

━━ Hub base URL: http://localhost:5001 ━━ (all actions are HTTP GET unless noted)

## Scenes  (coordinated lamp + TV)
{scene_lines}

## Lamp direct  GET /lamp/<endpoint>
Static:      on  off  focus  movie  sleep  relax  reading  romance
             dinner  morning  gaming  blue  brightness/<0-100>
Looping:     blink  pulse  party  alert  strobe  candle  campfire  aurora  disco
Transitions: sunset  sunrise  wake  bedtime  fade  goodnight

## TV  GET /tv/<endpoint>
Power:       on  off  status
Audio:       mute   volume/<n>  (positive = louder, negative = quieter; e.g. volume/5 or volume/-5)
Apps:        app/netflix   app/youtube   app/prime   app/spotify   app/appletv
Playback:    play  pause  stop  ff  rewind  next  prev
Navigation:  home  back  up  down  left  right  enter
Source:      source/hdmi1  source/hdmi2  source/hdmi3  source/hdmi4  source/tv  source/av

## Spotify  GET /spotify/<endpoint>
play  pause  next  prev  status
volume/<0-100>   shuffle/on   shuffle/off
repeat/off   repeat/track   repeat/context
search/<query>

## Beat sync  GET /spotify/beat-sync/<endpoint>
on  off  bpm/<n>

## Response format — strict JSON only, no markdown, no prose

Single action:
{{"action": "/scene/movie", "reason": "user wants to watch a movie"}}

Multiple actions (executed in order):
[
  {{"action": "/spotify/pause", "reason": "silence music first"}},
  {{"action": "/scene/focus",   "reason": "activate focus mode"}}
]

No-op (unclear or not a home-control request):
{{"action": null, "reason": "command unclear or no matching action"}}

## Matching rules
- Prefer /scene/* over separate /lamp + /tv calls — scenes handle both together
- Volume: "louder" → /tv/volume/10,  "a bit louder" → /tv/volume/5,  "quieter" → /tv/volume/-10
- Brightness: "dim it" → /lamp/brightness/20,  "brighter" → /lamp/brightness/80
- Filler words, coughs, background noise → action: null
- Never output anything other than valid JSON"""


# ---------------------------------------------------------------------------
# Whisper transcription
# ---------------------------------------------------------------------------

_whisper_model = None


def _load_whisper():
    global _whisper_model
    if _whisper_model is None:
        log.info("Loading Whisper '%s' model…", WHISPER_MODEL)
        _whisper_model = whisper.load_model(WHISPER_MODEL)
        log.info("Whisper ready.")
    return _whisper_model


def transcribe(pcm_frames: list) -> str:
    """Transcribe raw int16 PCM frames (16 kHz mono) with Whisper. Returns text."""
    raw = b"".join(pcm_frames)
    with _whisper_lock:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = f.name
        try:
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(CHANNELS)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(raw)
            result = _load_whisper().transcribe(wav_path, language="en", fp16=False)
            return result["text"].strip()
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Claude intent resolution
# ---------------------------------------------------------------------------

_claude_client = None
_system_prompt = None


def _get_client() -> anthropic.Anthropic:
    global _claude_client
    if _claude_client is None:
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set — add it to voice.env")
        _claude_client = anthropic.Anthropic(api_key=key)
    return _claude_client


def resolve_intent(transcript: str) -> list:
    """Send transcript to Claude; return list of {'action': ..., 'reason': ...} dicts."""
    raw = ""
    try:
        msg = _get_client().messages.create(
            model=MODEL,
            max_tokens=256,
            system=_system_prompt,
            messages=[{"role": "user", "content": transcript}],
        )
        raw = msg.content[0].text.strip()
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return [a for a in parsed if isinstance(a, dict)]
        return [{"action": None, "reason": "unexpected response shape"}]
    except json.JSONDecodeError:
        log.warning("Claude non-JSON response: %.120s", raw)
        return [{"action": None, "reason": "JSON parse error"}]
    except Exception as exc:
        log.error("Claude API error: %s", exc)
        return [{"action": None, "reason": str(exc)}]


# ---------------------------------------------------------------------------
# Hub dispatcher
# ---------------------------------------------------------------------------

def dispatch(actions: list):
    for item in actions:
        action = item.get("action")
        reason = item.get("reason", "")
        if not action:
            log.info("No-op — %s", reason)
            continue
        url = f"{HUB_URL}{action}"
        log.info("→ %-30s  %s", action, reason)
        try:
            r = requests.get(url, timeout=20)
            log.debug("   HTTP %d  %s", r.status_code, r.text[:100])
        except Exception as exc:
            log.error("   dispatch failed: %s", exc)


# ---------------------------------------------------------------------------
# Utterance handler (runs in background thread)
# ---------------------------------------------------------------------------

def _handle_utterance(frames: list):
    duration = len(frames) * FRAME_MS / 1000
    if duration < MIN_SPEECH_SEC:
        log.debug("Utterance too short (%.2fs) — skipped", duration)
        return

    log.info("Transcribing %.1fs…", duration)
    text = transcribe(frames)
    if not text:
        log.debug("Empty transcript — skipped")
        return

    log.info('Heard: "%s"', text)
    actions = resolve_intent(text)
    dispatch(actions)


# ---------------------------------------------------------------------------
# VAD audio capture loop
# ---------------------------------------------------------------------------

def audio_loop():
    vad = webrtcvad.Vad(VAD_MODE)
    pa  = pyaudio.PyAudio()

    open_kwargs = dict(
        rate=SAMPLE_RATE,
        channels=CHANNELS,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=FRAME_SAMPLES,
    )
    if INPUT_DEVICE != "":
        open_kwargs["input_device_index"] = int(INPUT_DEVICE)
    stream = pa.open(**open_kwargs)
    log.info("Microphone open — device=%s VAD aggressiveness=%d",
             INPUT_DEVICE or "default", VAD_MODE)

    pad_frames    = PADDING_MS // FRAME_MS
    ring          = deque(maxlen=pad_frames)
    voiced_frames = []
    in_speech     = False
    silence_frames = 0
    silence_limit  = round(SILENCE_TIMEOUT * 1000 / FRAME_MS)

    _dbg_window = []   # rolling 1-second window of is_speech bools

    try:
        while True:
            frame      = stream.read(FRAME_SAMPLES, exception_on_overflow=False)
            is_speech  = vad.is_speech(frame, SAMPLE_RATE)

            # Debug: log VAD result every ~1 s (33 frames × 30 ms)
            _dbg_window.append(is_speech)
            if len(_dbg_window) >= 33:
                n_speech = sum(_dbg_window)
                log.info("VAD window: %d/33 speech frames (in_speech=%s)", n_speech, in_speech)
                _dbg_window.clear()

            if not in_speech:
                ring.append(frame)
                if is_speech:
                    in_speech      = True
                    silence_frames = 0
                    voiced_frames  = list(ring)
                    log.info("Speech start detected")
            else:
                voiced_frames.append(frame)
                if is_speech:
                    silence_frames = 0
                else:
                    silence_frames += 1
                    if silence_frames >= silence_limit:
                        threading.Thread(
                            target=_handle_utterance,
                            args=(voiced_frames[:],),
                            daemon=True,
                        ).start()
                        voiced_frames  = []
                        in_speech      = False
                        silence_frames = 0
                        log.info("Speech end — dispatched to handler")
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down.")
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        log.error("ANTHROPIC_API_KEY is not set — add it to voice.env and restart")
        sys.exit(1)

    try:
        _system_prompt = build_system_prompt()
        log.info("System prompt ready (%d chars, %d scenes)",
                 len(_system_prompt),
                 len(json.loads(SCENES_FILE.read_text())))
    except Exception as exc:
        log.error("Failed to build system prompt: %s", exc)
        sys.exit(1)

    # Warm up Whisper at startup so the first utterance isn't slow
    try:
        _load_whisper()
    except Exception as exc:
        log.error("Failed to load Whisper model: %s", exc)
        sys.exit(1)

    audio_loop()
