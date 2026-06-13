"""
tts.py — Text-to-speech via Piper (neural) → PipeWire → JBL Flip 4.

Public API:
    speak(text, device="jbl")       — blocking TTS; returns True on success
    speak_async(text, device="jbl") — non-blocking daemon thread
    is_jbl_connected()              — True/False, cached 30s
    get_active_sink()               — "jbl" | "builtin" | "unknown"

Primary engine: Piper TTS (jenny-dioco, offline neural voice).
Fallback: espeak-ng. No cloud calls, no internet required.
JBL offline = silent skip. Never crashes. Never blocks hub shutdown.

TTS_ENABLED and TTS_MAX_WORDS are read live from hub.env on each call,
so /tts on/off takes effect immediately without restarting hub.

Scene guard: speech suppressed during movie, goodnight, sleep, dnd scenes.
Current scene is tracked in data/current_scene.json (written by hub.py).
"""

import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("tts")

_BASE    = Path(__file__).parent
_HUB_ENV = _BASE / "hub.env"
_SCENE_FILE = _BASE / "data" / "current_scene.json"

JBL_MAC = os.getenv("JBL_MAC", "6C:47:60:AA:21:DE")
_SUPPRESSED_SCENES = {"movie", "netflix", "goodnight", "sleep", "dnd"}

_PIPER_DIR   = _BASE / "piper" / "piper"
_PIPER_BIN   = _PIPER_DIR / "piper"
_PIPER_VOICE = _BASE / "piper" / "voices" / "en_GB-jenny_dioco-medium.onnx"

# JBL connection cache (30-second TTL)
_jbl_cache_lock = threading.Lock()
_jbl_cache_val  = False
_jbl_cache_ts   = 0.0
_JBL_CACHE_TTL  = 30.0

# ---------------------------------------------------------------------------
# hub.env live-read helpers (don't use os.getenv — it's stale after /tts cmd)
# ---------------------------------------------------------------------------

def _read_hub_env() -> dict:
    result = {}
    try:
        for line in _HUB_ENV.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip()
    except Exception:
        pass
    return result


def _is_tts_enabled() -> bool:
    return _read_hub_env().get("TTS_ENABLED", "false").lower() == "true"


def _max_words() -> int:
    try:
        return int(_read_hub_env().get("TTS_MAX_WORDS", "100"))
    except ValueError:
        return 100


def set_hub_env(key: str, value: str) -> bool:
    """Update a key in hub.env in-place, preserving all other lines."""
    try:
        lines = _HUB_ENV.read_text().splitlines()
        new_lines, found = [], False
        for line in lines:
            if line.strip().startswith(f"{key}="):
                new_lines.append(f"{key}={value}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"{key}={value}")
        _HUB_ENV.write_text("\n".join(new_lines) + "\n")
        return True
    except Exception as exc:
        log.error("set_hub_env failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Scene guard
# ---------------------------------------------------------------------------

_current_scene: str = ""
_scene_lock = threading.Lock()


def set_current_scene(scene: str) -> None:
    """Called by hub.py after each scene change."""
    global _current_scene
    with _scene_lock:
        _current_scene = scene
    try:
        _SCENE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SCENE_FILE.write_text(json.dumps({"scene": scene, "updated": time.time()}))
    except Exception:
        pass


def _get_current_scene() -> str:
    with _scene_lock:
        if _current_scene:
            return _current_scene
    # Fallback: read from shared file (for subprocess callers like tasks/)
    try:
        data = json.loads(_SCENE_FILE.read_text())
        if time.time() - data.get("updated", 0) < 600:  # trust for 10min
            return data.get("scene", "")
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# JBL connection check
# ---------------------------------------------------------------------------

def is_jbl_connected() -> bool:
    """Return True if JBL is connected over Bluetooth. Cached 30s."""
    global _jbl_cache_val, _jbl_cache_ts
    with _jbl_cache_lock:
        if time.monotonic() - _jbl_cache_ts < _JBL_CACHE_TTL:
            return _jbl_cache_val
    try:
        result = subprocess.run(
            ["bluetoothctl", "info", JBL_MAC],
            capture_output=True, text=True, timeout=5,
        )
        connected = "Connected: yes" in result.stdout
    except Exception:
        connected = False
    with _jbl_cache_lock:
        _jbl_cache_val = connected
        _jbl_cache_ts  = time.monotonic()
    return connected


def get_active_sink() -> str:
    """Return the active audio sink name."""
    try:
        env = _pw_env()
        result = subprocess.run(
            ["wpctl", "status"],
            capture_output=True, text=True, timeout=5, env=env,
        )
        for line in result.stdout.splitlines():
            if "*" in line and "JBL" in line:
                return "jbl"
            if "*" in line and ("Stereo" in line or "Audio" in line):
                return "builtin"
    except Exception:
        pass
    return "unknown"


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

def _clean(text: str, max_words: int = 100) -> str:
    """Strip markdown, emoji, URLs; truncate to max_words."""
    text = re.sub(r"\*+([^*]+)\*+", r"\1", text)          # bold/italic
    text = re.sub(r"_+([^_]+)_+", r"\1", text)            # underscore italic
    text = re.sub(r"`[^`]+`", "", text)                    # inline code
    text = re.sub(r"https?://\S+", "", text)               # URLs
    text = re.sub(r"[^\x00-\x7F]", " ", text)             # strip emoji / non-ASCII
    text = re.sub(r"\s+", " ", text).strip()
    words = text.split()
    if len(words) > max_words:
        words = words[:max_words]
    return " ".join(words)


# ---------------------------------------------------------------------------
# PipeWire environment
# ---------------------------------------------------------------------------

def _pw_env() -> dict:
    """Env dict with PipeWire session vars for subprocess calls."""
    env = os.environ.copy()
    uid = str(os.getuid())
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
    return env


# ---------------------------------------------------------------------------
# Core speak
# ---------------------------------------------------------------------------

def speak(text: str, device: str = "jbl") -> bool:
    """
    Speak text via Piper (neural) → PipeWire → JBL.
    Returns True on success. Never raises.
    """
    # Scene guard
    scene = _get_current_scene()
    if scene in _SUPPRESSED_SCENES:
        log.info("TTS: suppressed (%s scene active)", scene)
        return False

    # Device check
    if device == "jbl" and not is_jbl_connected():
        log.info("TTS: JBL offline, skipped")
        return False

    clean = _clean(text, _max_words())
    if not clean:
        return False

    env = _pw_env()
    env["LD_LIBRARY_PATH"] = str(_PIPER_DIR)

    # Primary: Piper neural TTS (jenny-dioco, warm British female)
    if _PIPER_BIN.exists() and _PIPER_VOICE.exists():
        try:
            piper = subprocess.Popen(
                [str(_PIPER_BIN), "--model", str(_PIPER_VOICE), "--length-scale", "1.1", "--output-raw"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            aplay = subprocess.Popen(
                ["aplay", "-r", "22050", "-f", "S16_LE", "-t", "raw", "-"],
                stdin=piper.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            piper.stdin.write(clean.encode())
            piper.stdin.close()
            piper.wait(timeout=60)
            aplay.wait(timeout=60)
            return True
        except Exception as exc:
            log.warning("TTS: Piper failed (%s), falling back to espeak-ng", exc)
    else:
        log.warning("TTS: Piper binary or voice not found at %s", _PIPER_DIR)

    # Fallback: espeak-ng
    try:
        subprocess.run(
            ["espeak-ng", "-v", "en-gb", "-s", "145", "-p", "40", "-a", "80", clean],
            timeout=30,
            env=env,
            capture_output=True,
        )
        return True
    except Exception as exc:
        log.error("TTS: espeak-ng fallback failed: %s", exc)

    return False


def speak_async(text: str, device: str = "jbl") -> None:
    """Non-blocking TTS in a daemon thread."""
    t = threading.Thread(target=speak, args=(text, device), daemon=True)
    t.start()
