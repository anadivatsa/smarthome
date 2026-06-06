#!/usr/bin/env python3
"""
Lamp Beat Sync — pulses WiZ lamp in time with music.

Spotify's audio analysis API is restricted to pre-Nov-2024 apps, so beat
timing is driven by a user-supplied (or auto-detected) BPM value rather than
per-track beat timestamps.

Controls (via hub.py):
  GET /spotify/beat-sync/on            start at current BPM (default 120)
  GET /spotify/beat-sync/off           stop
  GET /spotify/beat-sync/bpm/<n>       set BPM and (re)start
  GET /spotify/beat-sync/status        {"running", "bpm", "track"}
"""

import logging
import threading
import time

import requests

import spotify as SP

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_LAMP_BASE    = "http://localhost:5000"
_BRIGHT_HIGH  = 100   # % on the beat
_BRIGHT_LOW   = 15    # % between beats
_ON_FRACTION  = 0.30  # fraction of beat interval to stay bright
_RESYNC_EVERY = 8.0   # seconds between Spotify track polls

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_lock          = threading.Lock()
_stop_event    = threading.Event()
_thread        = None
_bpm           = 120.0
_current_track = None

# ---------------------------------------------------------------------------
# Lamp
# ---------------------------------------------------------------------------

def _lamp(pct: int):
    try:
        requests.get(f"{_LAMP_BASE}/brightness/{pct}", timeout=0.3)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Sync loop
# ---------------------------------------------------------------------------

def _beat_loop(stop: threading.Event):
    global _current_track
    last_resync = 0.0

    while not stop.is_set():
        now = time.monotonic()

        # Periodic Spotify poll — update track name, detect pause
        if now - last_resync >= _RESYNC_EVERY:
            status = SP.sp_status()
            with _lock:
                _current_track = status.get("track")
            if not status.get("playing"):
                # Paused — wait quietly
                while not stop.is_set() and not SP.sp_status().get("playing"):
                    stop.wait(2.0)
                if stop.is_set():
                    return
            last_resync = time.monotonic()

        with _lock:
            bpm = _bpm

        interval = 60.0 / bpm

        # Flash on beat
        _lamp(_BRIGHT_HIGH)
        on_time = interval * _ON_FRACTION
        if stop.wait(on_time):
            break

        _lamp(_BRIGHT_LOW)
        off_time = interval * (1.0 - _ON_FRACTION)
        if stop.wait(off_time):
            break

    _lamp(60)   # restore comfortable brightness on exit


def _start_thread():
    global _thread, _stop_event
    _stop_event = threading.Event()
    _thread = threading.Thread(target=_beat_loop, args=(_stop_event,), daemon=True)
    _thread.start()

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def bs_start() -> dict:
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return {"ok": True, "bpm": _bpm, "note": "already running"}
        _start_thread()
    return {"ok": True, "bpm": _bpm, "note": "beat sync started"}


def bs_stop() -> dict:
    global _thread
    with _lock:
        _stop_event.set()
        if _thread:
            _thread.join(timeout=3)
            _thread = None
    return {"ok": True, "note": "beat sync stopped"}


def bs_set_bpm(bpm: float) -> dict:
    global _bpm, _thread
    bpm = max(40.0, min(240.0, float(bpm)))
    with _lock:
        _bpm = bpm
        # Restart thread so new BPM takes effect immediately
        _stop_event.set()
        if _thread:
            _thread.join(timeout=2)
        _start_thread()
    return {"ok": True, "bpm": _bpm, "note": "beat sync running"}


def bs_status() -> dict:
    with _lock:
        running = bool(_thread and _thread.is_alive())
        return {"running": running, "bpm": _bpm, "track": _current_track}
