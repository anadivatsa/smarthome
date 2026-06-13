#!/usr/bin/env python3
"""
Spotify Web API client for Smart Home Hub.

Setup (one-time):
  1. Create an app at https://developer.spotify.com/dashboard
  2. Add redirect URI: http://192.168.1.8:5001/spotify/callback
  3. Copy Client ID + Secret into smarthome/spotify.env
  4. Visit http://192.168.1.8:5001/spotify/auth in a browser to authorise
"""

import base64
import json
import os
import secrets as _secrets
import threading
import time
import urllib.parse
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DIR = Path(__file__).parent

_env_file = _DIR / "spotify.env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
REDIRECT_URI  = os.getenv("SPOTIFY_REDIRECT_URI", "http://192.168.1.8:5001/spotify/callback")

_SCOPES = " ".join([
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "playlist-read-private",
    "user-library-modify",
    "user-library-read",
])

_TOKEN_FILE  = _DIR / "spotify_tokens.json"
_BASE        = "https://api.spotify.com/v1"
_oauth_state = ""  # set by sp_auth_url(), verified by sp_verify_state()

# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

_token_lock    = threading.Lock()
_access_token  = ""
_token_expiry  = 0.0


def _load_tokens():
    global _access_token, _token_expiry
    if _TOKEN_FILE.exists():
        try:
            data = json.loads(_TOKEN_FILE.read_text())
            with _token_lock:
                _access_token = data.get("access_token", "")
                _token_expiry = data.get("expires_at", 0.0)
        except Exception:
            pass


def _save_tokens(access_token: str, refresh_token: str, expires_in: int):
    global _access_token, _token_expiry
    expires_at = time.time() + expires_in - 60
    data = {"access_token": access_token, "refresh_token": refresh_token, "expires_at": expires_at}
    _TOKEN_FILE.write_text(json.dumps(data, indent=2))
    try:
        _TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    with _token_lock:
        _access_token = access_token
        _token_expiry = expires_at


def _do_refresh() -> bool:
    if not _TOKEN_FILE.exists():
        return False
    try:
        saved = json.loads(_TOKEN_FILE.read_text())
    except Exception:
        return False
    rt = saved.get("refresh_token", "")
    if not rt or not CLIENT_ID or not CLIENT_SECRET:
        return False
    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    try:
        r = requests.post(
            "https://accounts.spotify.com/api/token",
            headers={"Authorization": f"Basic {creds}"},
            data={"grant_type": "refresh_token", "refresh_token": rt},
            timeout=10,
        )
    except Exception:
        return False
    if r.status_code != 200:
        return False
    resp = r.json()
    _save_tokens(resp["access_token"], resp.get("refresh_token", rt), resp["expires_in"])
    return True


def _token() -> str:
    with _token_lock:
        if time.time() < _token_expiry:
            return _access_token
    if _do_refresh():
        with _token_lock:
            return _access_token
    return ""


def _headers() -> dict | None:
    t = _token()
    return {"Authorization": f"Bearer {t}"} if t else None


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(path: str, **kwargs):
    h = _headers()
    if h is None:
        return None, _auth_hint()
    try:
        r = requests.get(f"{_BASE}{path}", headers=h, timeout=8, **kwargs)
        if r.status_code == 204:
            return {}, None
        if r.status_code == 401 and _do_refresh():
            r = requests.get(f"{_BASE}{path}", headers=_headers(), timeout=8, **kwargs)
        if r.status_code == 404:
            return None, "No active Spotify device found"
        if not r.ok:
            return None, f"Spotify {r.status_code}: {r.text[:200]}"
        return (r.json() if r.content else {}), None
    except Exception as exc:
        return None, str(exc)


def _put(path: str, body=None):
    h = _headers()
    if h is None:
        return False, _auth_hint()
    if body is not None:
        h["Content-Type"] = "application/json"
    try:
        r = requests.put(f"{_BASE}{path}", headers=h, json=body, timeout=8)
        if r.status_code in (200, 204):
            return True, None
        if r.status_code == 404:
            return False, "No active Spotify device found"
        return False, f"Spotify {r.status_code}: {r.text[:200]}"
    except Exception as exc:
        return False, str(exc)


def _post(path: str, body=None):
    h = _headers()
    if h is None:
        return False, _auth_hint()
    if body is not None:
        h["Content-Type"] = "application/json"
    try:
        r = requests.post(f"{_BASE}{path}", headers=h, json=body, timeout=8)
        if r.status_code in (200, 201, 204):
            return True, None
        if r.status_code == 404:
            return False, "No active Spotify device found"
        return False, f"Spotify {r.status_code}: {r.text[:200]}"
    except Exception as exc:
        return False, str(exc)


def _auth_hint() -> str:
    if not CLIENT_ID or not CLIENT_SECRET:
        return "Spotify not configured — add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to smarthome/spotify.env"
    return "Not authenticated — visit http://192.168.1.8:5001/spotify/auth"


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

def sp_status() -> dict:
    data, err = _get("/me/player")
    if err:
        return {"error": err}
    if not data:
        return {"playing": False, "note": "No active device"}
    item    = data.get("item") or {}
    artists = ", ".join(a["name"] for a in item.get("artists", []))
    prog    = data.get("progress_ms", 0)
    dur     = item.get("duration_ms", 0)
    return {
        "playing":     data.get("is_playing", False),
        "track":       item.get("name"),
        "artist":      artists,
        "album":       (item.get("album") or {}).get("name"),
        "progress_ms": prog,
        "duration_ms": dur,
        "progress":    f"{prog//60000}:{(prog//1000)%60:02d}" if dur else None,
        "volume":      (data.get("device") or {}).get("volume_percent"),
        "device":      (data.get("device") or {}).get("name"),
        "shuffle":     data.get("shuffle_state"),
        "repeat":      data.get("repeat_state"),
        "uri":         item.get("uri"),
    }


def sp_play(uri: str | None = None) -> tuple[bool, str | None]:
    if uri and uri.startswith("spotify:track:"):
        body = {"uris": [uri]}
    elif uri:
        body = {"context_uri": uri}
    else:
        body = None
    return _put("/me/player/play", body)


def sp_pause() -> tuple[bool, str | None]:
    return _put("/me/player/pause")


def sp_next() -> tuple[bool, str | None]:
    return _post("/me/player/next")


def sp_prev() -> tuple[bool, str | None]:
    return _post("/me/player/previous")


def sp_volume(pct: int) -> tuple[bool, str | None]:
    pct = max(0, min(100, int(pct)))
    return _put(f"/me/player/volume?volume_percent={pct}")


def sp_shuffle(on: bool) -> tuple[bool, str | None]:
    return _put(f"/me/player/shuffle?state={'true' if on else 'false'}")


def sp_repeat(mode: str) -> tuple[bool, str | None]:
    if mode not in ("off", "track", "context"):
        return False, "mode must be off | track | context"
    return _put(f"/me/player/repeat?state={mode}")


def sp_seek(ms: int) -> tuple[bool, str | None]:
    return _put(f"/me/player/seek?position_ms={max(0, ms)}")


def sp_devices() -> dict:
    data, err = _get("/me/player/devices")
    if err:
        return {"error": err}
    return {"devices": [
        {"id": d["id"], "name": d["name"], "type": d["type"], "active": d["is_active"], "volume": d["volume_percent"]}
        for d in (data.get("devices") or [])
    ]}


def sp_transfer(device_id: str) -> tuple[bool, str | None]:
    return _put("/me/player", {"device_ids": [device_id], "play": True})


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def sp_audio_analysis(track_id: str) -> tuple[list | None, str | None]:
    data, err = _get(f"/audio-analysis/{track_id}")
    if err:
        return None, err
    return data.get("beats", []), None


def sp_search(query: str, limit: int = 5) -> tuple[dict | None, str | None]:
    data, err = _get("/search", params={"q": query, "type": "track,playlist,album", "limit": limit})
    if err:
        return None, err
    tracks = [
        {
            "name":        t["name"],
            "artist":      ", ".join(a["name"] for a in t["artists"]),
            "album":       t["album"]["name"],
            "uri":         t["uri"],
            "duration_ms": t["duration_ms"],
        }
        for t in (data.get("tracks") or {}).get("items", [])
    ]
    playlists = [
        {"name": p["name"], "owner": p["owner"]["display_name"], "uri": p["uri"]}
        for p in (data.get("playlists") or {}).get("items", []) if p
    ]
    albums = [
        {"name": a["name"], "artist": ", ".join(x["name"] for x in a["artists"]), "uri": a["uri"]}
        for a in (data.get("albums") or {}).get("items", [])
    ]
    return {"tracks": tracks, "playlists": playlists, "albums": albums}, None


# ---------------------------------------------------------------------------
# OAuth helpers (called from hub.py routes)
# ---------------------------------------------------------------------------

def sp_auth_url() -> str | None:
    global _oauth_state
    if not CLIENT_ID:
        return None
    _oauth_state = _secrets.token_urlsafe(16)
    params = {
        "client_id":     CLIENT_ID,
        "response_type": "code",
        "redirect_uri":  REDIRECT_URI,
        "scope":         _SCOPES,
        "show_dialog":   "false",
        "state":         _oauth_state,
    }
    return "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(params)


def sp_verify_state(received: str) -> bool:
    """Verify the OAuth state nonce to prevent CSRF on the callback."""
    return bool(_oauth_state) and _secrets.compare_digest(_oauth_state, received)


def sp_exchange_code(code: str) -> tuple[bool, str | None]:
    if not CLIENT_ID or not CLIENT_SECRET:
        return False, "CLIENT_ID / CLIENT_SECRET not configured"
    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    try:
        r = requests.post(
            "https://accounts.spotify.com/api/token",
            headers={"Authorization": f"Basic {creds}"},
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI},
            timeout=10,
        )
    except Exception as exc:
        return False, str(exc)
    if r.status_code != 200:
        return False, f"Token exchange failed ({r.status_code}): {r.text}"
    resp = r.json()
    _save_tokens(resp["access_token"], resp["refresh_token"], resp["expires_in"])
    return True, None


# Load saved tokens on import
_load_tokens()
