"""
Samsung TV controller — wraps samsungtvws for the hub.

TV: 192.168.1.2  (UA43DUE76AKLXL, Tizen, WebSocket on port 8002)
Token saved to ~/.smarthome/tv_token.json after first pairing.
"""

import json
import os
import time
import requests
from pathlib import Path

try:
    from samsungtvws import SamsungTVWS
except ImportError:
    SamsungTVWS = None

TV_IP   = os.getenv("TV_IP",   "192.168.1.2")
TV_PORT = int(os.getenv("TV_PORT", "8002"))
TV_NAME = "PiHub"
TOKEN_FILE = Path.home() / ".smarthome" / "tv_token.json"

# Confirmed installed apps (discovered via rest_app_status probe)
APPS = {
    "netflix":  "3201907018807",
    "youtube":  "111299001912",
    "prime":    "3201910019365",
    "spotify":  "3201606009684",
    "appletv":  "3201807016597",
}

# Source key codes
SOURCES = {
    "hdmi1": "KEY_HDMI1",
    "hdmi2": "KEY_HDMI2",
    "hdmi3": "KEY_HDMI3",
    "hdmi4": "KEY_HDMI4",
    "tv":    "KEY_TV",
    "av":    "KEY_AV",
}


def _token() -> str | None:
    if TOKEN_FILE.exists():
        try:
            return json.loads(TOKEN_FILE.read_text()).get("token")
        except Exception:
            pass
    return None


def _save_token(token: str):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({"token": token}))


def _connect():
    if SamsungTVWS is None:
        raise RuntimeError("samsungtvws not installed")
    return SamsungTVWS(
        host=TV_IP,
        port=TV_PORT,
        token=_token(),
        name=TV_NAME,
        timeout=10,
    )


def _persist_token(tv):
    token = getattr(tv, "token", None)
    if token:
        _save_token(token)


# ---------------------------------------------------------------------------
# Power
# ---------------------------------------------------------------------------

def tv_status() -> dict:
    try:
        r = requests.get(f"http://{TV_IP}:8001/api/v2/", timeout=4)
        device = r.json().get("device", {})
        return {
            "reachable": True,
            "name":      device.get("name"),
            "model":     device.get("modelName"),
            "power":     device.get("PowerState", "unknown"),
            "os":        device.get("OS"),
        }
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


def tv_on() -> tuple[bool, str | None]:
    return tv_key("KEY_POWER")


def tv_off() -> tuple[bool, str | None]:
    return tv_key("KEY_POWEROFF")


# ---------------------------------------------------------------------------
# Volume & audio
# ---------------------------------------------------------------------------

def tv_mute() -> tuple[bool, str | None]:
    return tv_key("KEY_MUTE")


def tv_set_volume(delta: int) -> tuple[bool, str | None]:
    """Raise or lower volume by |delta| steps. Positive = up, negative = down."""
    key   = "KEY_VOLUMEUP" if delta > 0 else "KEY_VOLUMEDOWN"
    steps = abs(delta)
    try:
        with _connect() as tv:
            for _ in range(steps):
                tv.send_key(key)
                time.sleep(0.12)
            _persist_token(tv)
        return True, None
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Source / input
# ---------------------------------------------------------------------------

def tv_source(name: str) -> tuple[bool, str | None]:
    key = SOURCES.get(name.lower())
    if not key:
        return False, f"Unknown source '{name}'. Available: {list(SOURCES)}"
    return tv_key(key)


# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------

def tv_launch_app(name_or_id: str) -> tuple[bool, str | None]:
    app_id = APPS.get(name_or_id.lower(), name_or_id)
    try:
        with _connect() as tv:
            tv.run_app(app_id)
            _persist_token(tv)
        return True, None
    except Exception as exc:
        return False, str(exc)


def tv_app_status(name_or_id: str) -> dict:
    app_id = APPS.get(name_or_id.lower(), name_or_id)
    try:
        with _connect() as tv:
            result = tv.rest_app_status(app_id)
            _persist_token(tv)
        return result
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

def tv_play()    -> tuple[bool, str | None]: return tv_key("KEY_PLAY")
def tv_pause()   -> tuple[bool, str | None]: return tv_key("KEY_PAUSE")
def tv_stop()    -> tuple[bool, str | None]: return tv_key("KEY_STOP")
def tv_ff()      -> tuple[bool, str | None]: return tv_key("KEY_FF")
def tv_rewind()  -> tuple[bool, str | None]: return tv_key("KEY_REWIND")
def tv_next()    -> tuple[bool, str | None]: return tv_key("KEY_NEXT")
def tv_prev()    -> tuple[bool, str | None]: return tv_key("KEY_PREVIOUS")


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

def tv_home()    -> tuple[bool, str | None]: return tv_key("KEY_HOME")
def tv_back()    -> tuple[bool, str | None]: return tv_key("KEY_RETURN")
def tv_up()      -> tuple[bool, str | None]: return tv_key("KEY_UP")
def tv_down()    -> tuple[bool, str | None]: return tv_key("KEY_DOWN")
def tv_left()    -> tuple[bool, str | None]: return tv_key("KEY_LEFT")
def tv_right()   -> tuple[bool, str | None]: return tv_key("KEY_RIGHT")
def tv_enter()   -> tuple[bool, str | None]: return tv_key("KEY_ENTER")


# ---------------------------------------------------------------------------
# Generic key (catch-all)
# ---------------------------------------------------------------------------

def tv_key(key: str) -> tuple[bool, str | None]:
    try:
        with _connect() as tv:
            tv.send_key(key)
            _persist_token(tv)
        return True, None
    except Exception as exc:
        return False, str(exc)
