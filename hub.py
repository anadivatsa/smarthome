#!/usr/bin/env python3
"""
Smart Home Hub — central Flask API (port 5001)

Scenes:     GET /scene/<name>   GET /scenes
TV power:   GET /tv/on  /tv/off  /tv/status
TV audio:   GET /tv/mute  /tv/volume/<n>
TV source:  GET /tv/source/<name>       hdmi1 hdmi2 hdmi3 hdmi4 tv av
TV apps:    GET /tv/app/<name>          youtube prime spotify appletv
TV play:    GET /tv/play  /tv/pause  /tv/stop  /tv/ff  /tv/rewind  /tv/next  /tv/prev
TV nav:     GET /tv/home  /tv/back  /tv/up  /tv/down  /tv/left  /tv/right  /tv/enter
TV raw:     GET /tv/key/<KEY_CODE>
Lamp:       GET /lamp/<path>            proxy to wiz-lamp on port 5000
Spotify:    GET /spotify/status
            GET /spotify/play           resume (or play a URI: ?uri=spotify:track:...)
            GET /spotify/pause
            GET /spotify/next
            GET /spotify/prev
            GET /spotify/volume/<0-100>
            GET /spotify/shuffle/<on|off>
            GET /spotify/repeat/<off|track|context>
            GET /spotify/search/<query>
            GET /spotify/devices
            GET /spotify/auth           → redirects to Spotify OAuth (first-time setup)
            GET /spotify/callback       OAuth callback (set as redirect URI in Spotify dashboard)
NFC tags:   POST /nfc/scan             {"uid": "<NFC identifier>"}  → executes scene
            POST /nfc/register         {"uid": "...", "scene": "..."}  → register tag
            GET  /nfc/tags             list registered tags + available scenes
            GET  /tag/<uid>            legacy GET trigger (browser/curl)
            GET  /tag/<uid>/<scene>    legacy register (browser/curl)
            GET  /tags                 legacy tag list
"""

import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
from flask import Flask, jsonify, request, send_from_directory
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import auth
import utils
import tv as TV
import spotify as SP
import beat_sync as BS
try:
    import tts as TTS
    _TTS_OK = True
except Exception:
    TTS = None
    _TTS_OK = False
try:
    import memory as MEM
    import scheduler as SCHED
    _MEM_OK   = True
    _SCHED_OK = True
except Exception as _import_err:
    import logging as _lg
    _lg.getLogger("hub").warning("Optional imports failed: %s", _import_err)
    MEM = None
    SCHED = None
    _MEM_OK   = False
    _SCHED_OK = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT          = int(os.getenv("HUB_PORT", "5001"))
HUB_IP        = os.getenv("HUB_IP", "") or utils.get_local_ip()
LAMP_BASE     = os.getenv("LAMP_URL", "http://localhost:5000")
SCENES_FILE   = Path(__file__).parent / "scenes.json"
TAGS_FILE     = Path(__file__).parent / "tags.json"
PRESENCE_FILE = Path(__file__).parent / "presence.json"

app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["120/minute"],
    storage_uri="memory://",
)


@app.before_request
def _check_auth():
    return auth.check_auth()


_DEPRECATION_NOTE = (
    "GET on state-changing endpoints is deprecated; "
    "switch to POST to suppress this header"
)


def _maybe_deprecate(response):
    """Add X-Deprecated header when a state-changing route is called via GET."""
    if request.method == "GET":
        response.headers["X-Deprecated"] = _DEPRECATION_NOTE
    return response


def _infer_trigger_source() -> str:
    """Guess who called the endpoint from request headers/params."""
    ua = request.headers.get("User-Agent", "").lower()
    if request.args.get("siri") == "1" or "shortcuts" in ua:
        return "siri"
    ref = request.headers.get("Referer", "")
    if ref:
        return "hub_dashboard"
    return "api"


def _log_scene(scene: str, triggered_by: str) -> None:
    """Best-effort scene event log — never raises."""
    if _MEM_OK:
        try:
            MEM.store_scene_event(scene, triggered_by)
        except Exception:
            pass


_scenes_cache: dict | None = None
_scenes_mtime: float = 0.0


def _load_scenes() -> dict:
    global _scenes_cache, _scenes_mtime
    mtime = SCENES_FILE.stat().st_mtime
    if _scenes_cache is None or mtime != _scenes_mtime:
        _scenes_cache = json.loads(SCENES_FILE.read_text())
        _scenes_mtime = mtime
    return _scenes_cache


_tags_cache: dict | None = None
_tags_mtime: float = 0.0


def _load_tags() -> dict:
    global _tags_cache, _tags_mtime
    mtime = TAGS_FILE.stat().st_mtime if TAGS_FILE.exists() else 0.0
    if _tags_cache is None or mtime != _tags_mtime:
        _tags_cache = json.loads(TAGS_FILE.read_text()).get("tags", {}) if TAGS_FILE.exists() else {}
        _tags_mtime = mtime
    return _tags_cache


def _save_tag(uid: str, scene: str):
    global _tags_cache, _tags_mtime
    data = json.loads(TAGS_FILE.read_text())
    data.setdefault("tags", {})[uid.upper()] = scene
    TAGS_FILE.write_text(json.dumps(data, indent=2))
    _tags_cache = None  # invalidate cache


def _get_presence() -> dict:
    if PRESENCE_FILE.exists():
        return json.loads(PRESENCE_FILE.read_text())
    return {"state": "unknown", "updated": None}


def _set_presence(state: str):
    PRESENCE_FILE.write_text(json.dumps(
        {"state": state, "updated": time.strftime("%Y-%m-%dT%H:%M:%S")},
        indent=2,
    ))


# ---------------------------------------------------------------------------
# Lamp proxy
# ---------------------------------------------------------------------------

def _lamp(endpoint: str) -> tuple[bool, dict]:
    try:
        r = requests.get(f"{LAMP_BASE}/{endpoint.lstrip('/')}", timeout=3)
        return True, r.json()
    except Exception as exc:
        return False, {"error": str(exc)}


# ---------------------------------------------------------------------------
# Scene execution  (lamp + TV run in parallel)
# ---------------------------------------------------------------------------

def _run_scene(name: str) -> tuple[bool, dict]:
    scenes = _load_scenes()
    if name not in scenes:
        return False, {"error": f"Unknown scene '{name}'", "available": list(scenes)}

    cfg     = scenes[name]
    lamp_ep = cfg.get("lamp")
    tv_cfg  = cfg.get("tv")
    results = {}
    errors  = []
    _lock   = threading.Lock()

    def _err(msg):
        with _lock:
            errors.append(msg)

    def _res(key, val):
        with _lock:
            results[key] = val

    def do_lamp():
        if lamp_ep:
            ok, data = _lamp(lamp_ep)
            _res("lamp", data)
            if not ok:
                _err(f"lamp: {data.get('error')}")

    def do_tv():
        if not tv_cfg:
            return
        action      = tv_cfg.get("action")
        volume      = tv_cfg.get("volume")       # relative delta
        volume_abs  = tv_cfg.get("volume_abs")   # absolute level 0-100
        app         = tv_cfg.get("app")
        playlist    = tv_cfg.get("playlist")     # deep-link URL/ID
        post_launch = tv_cfg.get("post_launch")
        volume_ramp = tv_cfg.get("volume_ramp")  # {"to": 60, "over": 120}

        if action == "off":
            TV.tv_stop_volume_ramp()
            ok, err = TV.tv_off()
            _res("tv_power", {"ok": ok, "error": err})
            if not ok:
                _err(f"tv: {err}")
            return

        if action == "on":
            ok, err = TV.tv_on()
            _res("tv_power", {"ok": ok, "error": err})
            if not ok:
                _err(f"tv: {err}")
                return
            if app or volume or volume_abs is not None:
                if not TV._wait_for_ws(timeout=15):
                    _res("tv_ready", {"ok": False, "error": "WebSocket not ready after power-on"})
                    _err("tv: WebSocket not ready after power-on")
                    return
                _res("tv_ready", {"ok": True})

        # Absolute volume first (zeroes out then counts up)
        if volume_abs is not None:
            ok, err = TV.tv_set_abs_volume(volume_abs)
            _res("tv_volume", {"ok": ok, "target": volume_abs, "error": err})
            if not ok:
                _err(f"tv volume: {err}")
        elif volume:
            ok, err = TV.tv_set_volume(volume)
            _res("tv_volume", {"ok": ok, "delta": volume, "error": err})
            if not ok:
                _err(f"tv volume: {err}")

        # Launch app with optional deep link
        if app:
            ok, err = TV.tv_launch_app(app, deep_link=playlist)
            _res("tv_app", {"ok": ok, "app": app, "playlist": playlist, "error": err})

            # Post-launch key sequence (e.g. auto-select Netflix profile)
            if ok and post_launch:
                time.sleep(post_launch.get("delay", 3))
                for key in post_launch.get("keys", []):
                    TV.tv_key(key)
                    time.sleep(0.3)

            if ok and volume_ramp:
                start_vol = volume_abs if volume_abs is not None else (volume or 0)
                time.sleep(volume_ramp.get("delay", 3))
                TV.tv_start_volume_ramp(
                    from_vol    = start_vol,
                    to_vol      = volume_ramp["to"],
                    over_seconds= volume_ramp.get("over", 120),
                )

    def do_spotify():
        sp_cfg = cfg.get("spotify")
        if not sp_cfg:
            return
        uri    = sp_cfg.get("uri")
        volume = sp_cfg.get("volume")
        delay  = sp_cfg.get("delay", 12)   # wait for Spotify app to load on TV
        time.sleep(delay)
        if volume is not None:
            SP.sp_volume(volume)
        ok, err = SP.sp_play(uri)
        _res("spotify", {"ok": ok, "uri": uri, "error": err})
        if not ok:
            _err(f"spotify: {err}")

    t_lamp    = threading.Thread(target=do_lamp)
    t_tv      = threading.Thread(target=do_tv)
    t_spotify = threading.Thread(target=do_spotify)
    t_lamp.start(); t_tv.start(); t_spotify.start()
    t_lamp.join();  t_tv.join();  t_spotify.join()

    if presence_state := cfg.get("presence"):
        _set_presence(presence_state)
        _res("presence", presence_state)

    ok = len(errors) == 0
    if ok and _TTS_OK:
        try:
            TTS.set_current_scene(name)
        except Exception:
            pass
    return ok, {"scene": name, "results": results, "errors": errors}


# ---------------------------------------------------------------------------
# Scene routes
# ---------------------------------------------------------------------------

@app.route("/scene/<name>", methods=["GET", "POST"])
@limiter.limit("10/minute")
def route_scene(name):
    ok, data = _run_scene(name)
    if ok:
        _log_scene(name, _infer_trigger_source())
    resp = jsonify(data), (200 if ok else 207)
    return _maybe_deprecate(resp[0]), resp[1]


@app.route("/scenes")
def route_scenes():
    scenes = _load_scenes()
    return jsonify({"scenes": list(scenes.keys()), "count": len(scenes)})


# ---------------------------------------------------------------------------
# TV — power
# ---------------------------------------------------------------------------

@app.route("/tv/status")
@limiter.limit("30/minute")
def route_tv_status():
    return jsonify(TV.tv_status())


@app.route("/tv/on", methods=["GET", "POST"])
@limiter.limit("30/minute")
def route_tv_on():
    ok, err = TV.tv_on()
    return _maybe_deprecate(jsonify({"ok": ok, "error": err}))


@app.route("/tv/off", methods=["GET", "POST"])
@limiter.limit("30/minute")
def route_tv_off():
    ok, err = TV.tv_off()
    return _maybe_deprecate(jsonify({"ok": ok, "error": err}))


# ---------------------------------------------------------------------------
# TV — audio
# ---------------------------------------------------------------------------

@app.route("/tv/mute", methods=["GET", "POST"])
@limiter.limit("30/minute")
def route_tv_mute():
    ok, err = TV.tv_mute()
    return _maybe_deprecate(jsonify({"ok": ok, "action": "mute_toggle", "error": err}))


@app.route("/tv/volume/<int:delta>", methods=["GET", "POST"])
@limiter.limit("30/minute")
def route_tv_volume(delta):
    ok, err = TV.tv_set_volume(delta)
    return _maybe_deprecate(
        jsonify({"ok": ok, "delta": delta, "direction": "up" if delta > 0 else "down", "error": err})
    )


# ---------------------------------------------------------------------------
# TV — source
# ---------------------------------------------------------------------------

@app.route("/tv/source/<name>", methods=["GET", "POST"])
@limiter.limit("30/minute")
def route_tv_source(name):
    ok, err = TV.tv_source(name)
    return _maybe_deprecate(
        jsonify({"ok": ok, "source": name, "error": err, "available": list(TV.SOURCES.keys())})
    )


# ---------------------------------------------------------------------------
# TV — apps
# ---------------------------------------------------------------------------

@app.route("/tv/app/<name>", methods=["GET", "POST"])
@limiter.limit("30/minute")
def route_tv_app(name):
    ok, err = TV.tv_launch_app(name)
    return _maybe_deprecate(
        jsonify({"ok": ok, "app": name, "installed": list(TV.APPS.keys()), "error": err})
    )


@app.route("/tv/apps")
def route_tv_apps():
    return jsonify({"installed": TV.APPS})


# ---------------------------------------------------------------------------
# TV — playback
# ---------------------------------------------------------------------------

@app.route("/tv/play",   methods=["GET", "POST"])
@limiter.limit("30/minute")
def route_tv_play():    return _maybe_deprecate(jsonify({"ok": TV.tv_play()[0]}))

@app.route("/tv/pause",  methods=["GET", "POST"])
@limiter.limit("30/minute")
def route_tv_pause():   return _maybe_deprecate(jsonify({"ok": TV.tv_pause()[0]}))

@app.route("/tv/stop",   methods=["GET", "POST"])
@limiter.limit("30/minute")
def route_tv_stop():    return _maybe_deprecate(jsonify({"ok": TV.tv_stop()[0]}))

@app.route("/tv/ff",     methods=["GET", "POST"])
@limiter.limit("30/minute")
def route_tv_ff():      return _maybe_deprecate(jsonify({"ok": TV.tv_ff()[0]}))

@app.route("/tv/rewind", methods=["GET", "POST"])
@limiter.limit("30/minute")
def route_tv_rewind():  return _maybe_deprecate(jsonify({"ok": TV.tv_rewind()[0]}))

@app.route("/tv/next",   methods=["GET", "POST"])
@limiter.limit("30/minute")
def route_tv_next():    return _maybe_deprecate(jsonify({"ok": TV.tv_next()[0]}))

@app.route("/tv/prev",   methods=["GET", "POST"])
@limiter.limit("30/minute")
def route_tv_prev():    return _maybe_deprecate(jsonify({"ok": TV.tv_prev()[0]}))


# ---------------------------------------------------------------------------
# TV — navigation
# ---------------------------------------------------------------------------

@app.route("/tv/home",  methods=["GET", "POST"])
@limiter.limit("30/minute")
def route_tv_home():    return _maybe_deprecate(jsonify({"ok": TV.tv_home()[0]}))

@app.route("/tv/back",  methods=["GET", "POST"])
@limiter.limit("30/minute")
def route_tv_back():    return _maybe_deprecate(jsonify({"ok": TV.tv_back()[0]}))

@app.route("/tv/up",    methods=["GET", "POST"])
@limiter.limit("30/minute")
def route_tv_up():      return _maybe_deprecate(jsonify({"ok": TV.tv_up()[0]}))

@app.route("/tv/down",  methods=["GET", "POST"])
@limiter.limit("30/minute")
def route_tv_down():    return _maybe_deprecate(jsonify({"ok": TV.tv_down()[0]}))

@app.route("/tv/left",  methods=["GET", "POST"])
@limiter.limit("30/minute")
def route_tv_left():    return _maybe_deprecate(jsonify({"ok": TV.tv_left()[0]}))

@app.route("/tv/right", methods=["GET", "POST"])
@limiter.limit("30/minute")
def route_tv_right():   return _maybe_deprecate(jsonify({"ok": TV.tv_right()[0]}))

@app.route("/tv/enter", methods=["GET", "POST"])
@limiter.limit("30/minute")
def route_tv_enter():   return _maybe_deprecate(jsonify({"ok": TV.tv_enter()[0]}))


# ---------------------------------------------------------------------------
# TV — raw key (catch-all)
# ---------------------------------------------------------------------------

@app.route("/tv/key/<key>", methods=["GET", "POST"])
@limiter.limit("30/minute")
def route_tv_key(key):
    if key not in TV.KEY_WHITELIST:
        return jsonify({"ok": False, "key": key,
                        "error": f"Key '{key}' not in whitelist",
                        "allowed": sorted(TV.KEY_WHITELIST)}), 400
    ok, err = TV.tv_key(key)
    return _maybe_deprecate(jsonify({"ok": ok, "key": key, "error": err}))


# ---------------------------------------------------------------------------
# Lamp proxy
# ---------------------------------------------------------------------------

@app.route("/lamp/", defaults={"path": ""})
@app.route("/lamp/<path:path>", methods=["GET", "POST"])
@limiter.limit("60/minute")
def route_lamp(path):
    ok, data = _lamp(path)
    return _maybe_deprecate(jsonify(data)), (200 if ok else 502)


# ---------------------------------------------------------------------------
# Spotify
# ---------------------------------------------------------------------------

@app.route("/spotify/status")
@limiter.limit("20/minute")
def route_sp_status():
    return jsonify(SP.sp_status())


@app.route("/spotify/play", methods=["GET", "POST"])
@limiter.limit("20/minute")
def route_sp_play():
    uri = request.args.get("uri") or (request.get_json(silent=True) or {}).get("uri")
    ok, err = SP.sp_play(uri)
    return _maybe_deprecate(jsonify({"ok": ok, "uri": uri, "error": err}))


@app.route("/spotify/pause", methods=["GET", "POST"])
@limiter.limit("20/minute")
def route_sp_pause():
    ok, err = SP.sp_pause()
    return _maybe_deprecate(jsonify({"ok": ok, "error": err}))


@app.route("/spotify/next", methods=["GET", "POST"])
@limiter.limit("20/minute")
def route_sp_next():
    ok, err = SP.sp_next()
    return _maybe_deprecate(jsonify({"ok": ok, "error": err}))


@app.route("/spotify/prev", methods=["GET", "POST"])
@limiter.limit("20/minute")
def route_sp_prev():
    ok, err = SP.sp_prev()
    return _maybe_deprecate(jsonify({"ok": ok, "error": err}))


@app.route("/spotify/volume/<int:pct>", methods=["GET", "POST"])
@limiter.limit("20/minute")
def route_sp_volume(pct):
    ok, err = SP.sp_volume(pct)
    return _maybe_deprecate(jsonify({"ok": ok, "volume": pct, "error": err}))


@app.route("/spotify/shuffle/<state>", methods=["GET", "POST"])
@limiter.limit("20/minute")
def route_sp_shuffle(state):
    if state not in ("on", "off"):
        return jsonify({"error": "state must be on or off"}), 400
    ok, err = SP.sp_shuffle(state == "on")
    return _maybe_deprecate(jsonify({"ok": ok, "shuffle": state, "error": err}))


@app.route("/spotify/repeat/<mode>", methods=["GET", "POST"])
@limiter.limit("20/minute")
def route_sp_repeat(mode):
    ok, err = SP.sp_repeat(mode)
    return _maybe_deprecate(jsonify({"ok": ok, "repeat": mode, "error": err}))


@app.route("/spotify/search/<path:query>")
@limiter.limit("20/minute")
def route_sp_search(query):
    limit = int(request.args.get("limit", 5))
    data, err = SP.sp_search(query, limit=limit)
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data)


@app.route("/spotify/beat-sync/on", methods=["GET", "POST"])
@limiter.limit("20/minute")
def route_bs_on():
    return _maybe_deprecate(jsonify(BS.bs_start()))


@app.route("/spotify/beat-sync/off", methods=["GET", "POST"])
@limiter.limit("20/minute")
def route_bs_off():
    return _maybe_deprecate(jsonify(BS.bs_stop()))


@app.route("/spotify/beat-sync/status")
@limiter.limit("20/minute")
def route_bs_status():
    return jsonify(BS.bs_status())


@app.route("/spotify/beat-sync/bpm/<int:bpm>", methods=["GET", "POST"])
@limiter.limit("20/minute")
def route_bs_bpm(bpm):
    return _maybe_deprecate(jsonify(BS.bs_set_bpm(bpm)))


@app.route("/spotify/devices")
@limiter.limit("20/minute")
def route_sp_devices():
    return jsonify(SP.sp_devices())


@app.route("/spotify/auth")
def route_sp_auth():
    from flask import redirect
    url = SP.sp_auth_url()
    if not url:
        return jsonify({"error": "Spotify not configured — add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to smarthome/spotify.env"}), 503
    return redirect(url)


@app.route("/spotify/callback")
def route_sp_callback():
    error = request.args.get("error")
    if error:
        return jsonify({"error": f"Spotify auth denied: {error}"}), 400
    state = request.args.get("state", "")
    if not SP.sp_verify_state(state):
        return jsonify({"error": "OAuth state mismatch — possible CSRF, try /spotify/auth again"}), 400
    code = request.args.get("code", "")
    ok, err = SP.sp_exchange_code(code)
    if not ok:
        return jsonify({"error": err}), 500
    return jsonify({"ok": True, "message": "Spotify authenticated — you can close this tab."})


@app.route("/spotify/exchange")
def route_sp_exchange():
    """Manual code exchange for headless auth — paste code from the browser URL bar."""
    code = request.args.get("code", "").strip()
    if not code:
        return jsonify({"error": "Provide ?code=<auth_code> from the redirect URL"}), 400
    ok, err = SP.sp_exchange_code(code)
    if not ok:
        return jsonify({"error": err}), 500
    return jsonify({"ok": True, "message": "Spotify authenticated successfully."})


# ---------------------------------------------------------------------------
# NFC helpers
# ---------------------------------------------------------------------------

def _normalize_uid(uid: str) -> str:
    return uid.upper().replace(":", "").replace("-", "").strip()


def _trigger_tag(uid: str, triggered_by: str = "nfc") -> tuple[int, dict]:
    """Look up uid in tags.json and run the mapped scene. Always returns 200."""
    uid   = _normalize_uid(uid)
    scene = _load_tags().get(uid)
    if not scene:
        return 200, {
            "triggered":  False,
            "registered": False,
            "uid":        uid,
            "scenes":     list(_load_scenes().keys()),
        }
    ok, data = _run_scene(scene)
    if ok:
        _log_scene(scene, triggered_by)
    data.update({"triggered": True, "uid": uid, "scene": scene})
    return (200 if ok else 207), data


# ---------------------------------------------------------------------------
# NFC routes — iPhone Shortcuts
# ---------------------------------------------------------------------------

@app.route("/nfc/scan", methods=["POST"])
@limiter.limit("5/minute")
def route_nfc_scan():
    """iOS Shortcuts calls POST /nfc/scan with {"uid": "<NFC Tag Identifier>"}."""
    body = request.get_json(silent=True) or {}
    uid  = body.get("uid", "").strip()
    if not uid:
        return jsonify({"error": "uid is required"}), 400
    status, data = _trigger_tag(uid)
    return jsonify(data), status


@app.route("/nfc/register", methods=["POST"])
@limiter.limit("5/minute")
def route_nfc_register():
    """Register a tag — POST {"uid": "...", "scene": "movie"}."""
    body  = request.get_json(silent=True) or {}
    uid   = body.get("uid", "").strip()
    scene = body.get("scene", "").strip()
    if not uid or not scene:
        return jsonify({"error": "uid and scene are required"}), 400
    uid    = _normalize_uid(uid)
    scenes = _load_scenes()
    if scene not in scenes:
        return jsonify({"error": f"Unknown scene '{scene}'", "available": list(scenes)}), 400
    _save_tag(uid, scene)
    return jsonify({"registered": True, "uid": uid, "scene": scene})


@app.route("/nfc/tags")
@limiter.limit("5/minute")
def route_nfc_tags():
    return jsonify({"tags": _load_tags(), "scenes": list(_load_scenes().keys())})


# ---------------------------------------------------------------------------
# NFC tag routes — legacy (GET, browser/curl friendly)
# ---------------------------------------------------------------------------

@app.route("/tag/<uid>")
@limiter.limit("5/minute")
def route_tag(uid):
    status, data = _trigger_tag(uid)
    if not data.get("registered", True) and not data.get("triggered"):
        data["hint"] = f"Register with POST /nfc/register or GET /tag/{_normalize_uid(uid)}/<scene>"
        return jsonify(data), 404
    return jsonify(data), status


@app.route("/tag/<uid>/<scene>", methods=["GET", "POST"])
@limiter.limit("5/minute")
def route_tag_register(uid, scene):
    uid    = _normalize_uid(uid)
    scenes = _load_scenes()
    if scene not in scenes:
        return jsonify({"error": f"Unknown scene '{scene}'", "available": list(scenes)}), 400
    _save_tag(uid, scene)
    return jsonify({"registered": True, "uid": uid, "scene": scene})


@app.route("/tags")
@limiter.limit("5/minute")
def route_tags():
    return jsonify({"tags": _load_tags(), "scenes": list(_load_scenes().keys())})


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------

@app.route("/presence", methods=["GET", "POST"])
def route_presence():
    if request.method == "POST":
        body  = request.get_json(silent=True) or {}
        state = body.get("state", "").strip()
        if state not in ("home", "away"):
            return jsonify({"error": "state must be 'home' or 'away'"}), 400
        _set_presence(state)
    return jsonify(_get_presence())


# ---------------------------------------------------------------------------
# Siri Shortcuts reference page
# ---------------------------------------------------------------------------

@app.route("/shortcuts")
def route_shortcuts():
    base = f"http://{HUB_IP}:{PORT}"
    return jsonify({
        "instructions": "In the Shortcuts app: New Shortcut → Add Action → 'Get Contents of URL' → paste URL → Add to Siri → record phrase",
        "scenes": {phrase: f"{base}/scene/{scene}" for phrase, scene in {
            "Movie time":    "movie",
            "Party time":    "party",
            "Play music":    "music",
            "Good night":    "goodnight",
            "Focus mode":    "focus",
            "Morning":       "morning",
            "Dinner time":   "dinner",
            "Gaming mode":   "gaming",
            "Romance mode":  "romance",
            "Turn off":      "off",
        }.items()},
        "spotify": {
            "Pause music":   f"{base}/spotify/pause",
            "Resume music":  f"{base}/spotify/play",
            "Next song":     f"{base}/spotify/next",
            "Previous song": f"{base}/spotify/prev",
        },
        "lamp": {
            "Lights on":     f"{base}/lamp/on",
            "Lights off":    f"{base}/lamp/off",
            "Disco mode":    f"{base}/lamp/disco",
            "Relax light":   f"{base}/lamp/relax",
            "Bright light":  f"{base}/lamp/focus",
        },
        "tv": {
            "TV on":         f"{base}/tv/on",
            "TV off":        f"{base}/tv/off",
        },
    })


# ---------------------------------------------------------------------------
# Info / health endpoint
# ---------------------------------------------------------------------------

@app.route("/api/info")
@limiter.limit("30/minute")
def route_api_info():
    import subprocess as _sp  # noqa: PLC0415
    def _svc(name):
        try:
            r = _sp.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=3)
            return r.stdout.strip()
        except Exception:
            return "unknown"

    def _port_listening(port):
        import socket as _sock  # noqa: PLC0415
        try:
            s = _sock.create_connection(("127.0.0.1", port), timeout=1)
            s.close()
            return "running"
        except Exception:
            return "not reachable"

    ts_ip    = utils.get_tailscale_ip()
    local_ip = utils.get_local_ip()
    audio_block: dict = {"tts_enabled": os.getenv("TTS_ENABLED", "false")}
    if _TTS_OK:
        try:
            audio_block["jbl_connected"] = TTS.is_jbl_connected()
            audio_block["jbl_mac"]       = TTS.JBL_MAC
            audio_block["active_sink"]   = TTS.get_active_sink()
            audio_block["tts_enabled"]   = TTS._is_tts_enabled()
        except Exception:
            pass
    return jsonify({
        "hostname":           utils.HOSTNAME,
        "local_ip":           local_ip,
        "tailscale_ip":       ts_ip or None,
        "hub_url_local":      f"http://{local_ip}:{PORT}",
        "hub_url_tailscale":  f"http://{ts_ip}:{PORT}" if ts_ip else None,
        "version":            utils.git_version(),
        "uptime_seconds":     round(utils.uptime_seconds()),
        "services": {
            "hub":      _svc("hub"),
            "tgvoice":  _svc("tgvoice"),
            "voice":    _svc("voice"),
            "wiz_lamp": _port_listening(5000),
        },
        "audio": audio_block,
    })


# ---------------------------------------------------------------------------
# Announce endpoint (Task 5)
# ---------------------------------------------------------------------------

@app.route("/api/announce", methods=["POST"])
@limiter.limit("10/minute")
def route_api_announce():
    if not _TTS_OK:
        return jsonify({"error": "tts module not available"}), 503
    body = request.get_json(silent=True) or {}
    text = body.get("text", "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400
    if len(text) > 500:
        return jsonify({"error": "text must be < 500 chars"}), 400
    device = body.get("device", "jbl")
    TTS.speak_async(text, device)
    if _MEM_OK:
        try:
            MEM.store_memory(f"Announcement: {text}", role="announcement", source="api")
        except Exception:
            pass
    return jsonify({
        "status":        "speaking",
        "text":          text,
        "jbl_connected": TTS.is_jbl_connected(),
    })


# ---------------------------------------------------------------------------
# TTS toggle / status endpoints
# ---------------------------------------------------------------------------

@app.route("/tts/on")
@limiter.limit("20/minute")
def route_tts_on():
    if not _TTS_OK:
        return jsonify({"error": "tts module not available"}), 503
    TTS.set_hub_env("TTS_ENABLED", "true")
    return jsonify({"tts_enabled": True, "jbl_connected": TTS.is_jbl_connected()})


@app.route("/tts/off")
@limiter.limit("20/minute")
def route_tts_off():
    if not _TTS_OK:
        return jsonify({"error": "tts module not available"}), 503
    TTS.set_hub_env("TTS_ENABLED", "false")
    return jsonify({"tts_enabled": False, "jbl_connected": TTS.is_jbl_connected()})


@app.route("/tts/status")
@limiter.limit("30/minute")
def route_tts_status():
    if not _TTS_OK:
        return jsonify({"error": "tts module not available"}), 503
    return jsonify({
        "tts_enabled":  TTS._is_tts_enabled(),
        "jbl_connected": TTS.is_jbl_connected(),
        "jbl_mac":      TTS.JBL_MAC,
        "active_sink":  TTS.get_active_sink(),
    })


# ---------------------------------------------------------------------------
# Voice API — POST /api/voice  (Whisper → Claude intent → dispatch)
# ---------------------------------------------------------------------------

_vapi_log     = logging.getLogger("hub")
_vapi_whisper = None
_vapi_lock    = threading.Semaphore(1)
_vapi_claude  = None

_VAPI_PROMPT = """\
You are the voice controller for a smart home hub (Raspberry Pi).
Parse the voice transcript and return JSON action(s) to execute.

## Scenes  (GET /scene/<name>)
{scenes}

## Lamp  GET /lamp/<endpoint>
Static:      on off focus movie sleep relax reading romance dinner morning gaming blue brightness/<0-100>
Effects:     blink pulse party alert strobe candle campfire aurora disco
Transitions: sunset sunrise wake bedtime fade goodnight

## TV  GET /tv/<endpoint>
Power: on off  Audio: mute volume/<n>  Apps: app/netflix app/youtube app/prime app/spotify
Playback: play pause stop ff rewind next prev  Nav: home back up down left right enter

## Spotify  GET /spotify/<endpoint>
play pause next prev volume/<0-100> shuffle/on shuffle/off search/<q>

## Response — strict JSON only, no markdown, no prose
Single: {{"action": "/scene/movie", "reason": "user wants to watch a movie"}}
Multi:  [{{"action": "/spotify/pause", "reason": "..."}}, {{"action": "/scene/focus", "reason": "..."}}]
No-op:  {{"action": null, "reason": "unclear or not a home-control request"}}\
"""


def _vapi_build_prompt() -> str:
    scenes = json.loads(SCENES_FILE.read_text())
    def _summary(n, c):
        tv = c.get("tv", {})
        parts = [f"lamp={c.get('lamp','—')}", f"tv={tv.get('action','—')}"]
        if tv.get("app"):      parts.append(f"app={tv['app']}")
        if c.get("spotify"):   parts.append("spotify=play")
        if c.get("presence"):  parts.append(f"presence={c['presence']}")
        return ", ".join(parts)
    scene_lines = "\n".join(f"  /scene/{n}  ({_summary(n,c)})" for n, c in scenes.items())
    return _VAPI_PROMPT.format(scenes=scene_lines)


def _vapi_get_whisper():
    global _vapi_whisper
    if _vapi_whisper is None:
        _vapi_log.info("Voice API: loading Whisper…")
        import whisper as _w
        _vapi_whisper = _w.load_model(os.getenv("WHISPER_MODEL", "base"))
        _vapi_log.info("Voice API: Whisper ready")
    return _vapi_whisper


def _vapi_get_claude():
    global _vapi_claude
    if _vapi_claude is None:
        import anthropic as _a
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            try:
                for line in (Path(__file__).parent / "voice.env").read_text().splitlines():
                    if line.startswith("ANTHROPIC_API_KEY="):
                        key = line.split("=", 1)[1].strip()
                        break
            except Exception:
                pass
        _vapi_claude = _a.Anthropic(api_key=key)
    return _vapi_claude


@app.route("/api/voice", methods=["POST"])
@limiter.limit("30/minute")
def route_api_voice():
    """Accept WAV audio, transcribe with Whisper, dispatch via Claude intent."""
    if "audio" not in request.files:
        return jsonify({"error": "multipart field 'audio' required"}), 400

    audio_file = request.files["audio"]
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        audio_file.save(tmp.name)
        tmp.close()

        with _vapi_lock:
            result = _vapi_get_whisper().transcribe(tmp.name, language="en", fp16=False)
        transcript = result["text"].strip()

        if not transcript:
            return jsonify({"transcript": "", "actions": [], "reason": "empty transcript"})

        _vapi_log.info("Voice API: heard %r", transcript)

        raw = ""
        try:
            msg = _vapi_get_claude().messages.create(
                model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
                max_tokens=256,
                system=_vapi_build_prompt(),
                messages=[{"role": "user", "content": transcript}],
            )
            raw = msg.content[0].text.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw).strip()
            parsed = json.loads(raw)
            actions = [parsed] if isinstance(parsed, dict) else [a for a in parsed if isinstance(a, dict)]
        except Exception as exc:
            _vapi_log.warning("Voice API: Claude error: %s  raw=%r", exc, raw[:80])
            return jsonify({"transcript": transcript, "actions": [], "reason": str(exc)})

        headers = {"X-Neo-Key": os.getenv("NEO_API_KEY", "")}
        dispatched = []
        for item in actions:
            action = item.get("action")
            reason = item.get("reason", "")
            if not action:
                dispatched.append({"action": None, "reason": reason})
                continue
            url = f"http://localhost:{PORT}{action}"
            _vapi_log.info("Voice API → %-30s  %s", action, reason)
            try:
                r = requests.get(url, headers=headers, timeout=20)
                dispatched.append({"action": action, "reason": reason, "status": r.status_code})
            except Exception as exc:
                dispatched.append({"action": action, "reason": reason, "error": str(exc)})

        return jsonify({"transcript": transcript, "actions": dispatched})

    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Memory API endpoints
# ---------------------------------------------------------------------------

@app.route("/api/memory")
@limiter.limit("30/minute")
def route_api_memory():
    if not _MEM_OK:
        return jsonify({"error": "memory module not available"}), 503
    q = request.args.get("q", "").strip()
    n = min(int(request.args.get("n", 5)), 20)
    if not q:
        return jsonify({"error": "Provide ?q=<query>"}), 400
    try:
        results = MEM.search(q, n=n)
        return jsonify({"results": results})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/scene_log")
@limiter.limit("30/minute")
def route_api_scene_log():
    if not _MEM_OK:
        return jsonify({"error": "memory module not available"}), 503
    n = min(int(request.args.get("n", 20)), 100)
    try:
        log_entries = MEM.get_scene_history(n=n)
        return jsonify({"log": log_entries})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/diary")
@limiter.limit("30/minute")
def route_api_diary():
    if not _MEM_OK:
        return jsonify({"error": "memory module not available"}), 503
    n = min(int(request.args.get("n", 7)), 30)
    try:
        c = MEM._conn()
        rows = c.execute(
            "SELECT source, content, timestamp FROM memories "
            "WHERE role = 'diary' ORDER BY timestamp DESC LIMIT ?", (n,)
        ).fetchall()
        entries = []
        for row in rows:
            src  = row["source"] or ""
            date = src.replace("neo_diary_", "") if src.startswith("neo_diary_") else row["timestamp"][:10]
            entries.append({
                "date":      date,
                "content":   row["content"],
                "timestamp": row["timestamp"],
            })
        return jsonify({"entries": entries})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Health / index
# ---------------------------------------------------------------------------

@app.route("/")
def route_index():
    return send_from_directory(Path(__file__).parent, "dashboard.html")


@app.route("/api")
def route_api():
    return jsonify({
        "service": "Smart Home Hub",
        "version": "1.1",
        "tv": {
            "ip": TV.TV_IP,
            "apps": list(TV.APPS.keys()),
            "sources": list(TV.SOURCES.keys()),
        },
        "endpoints": {
            "scenes":    "/scene/<name>  /scenes",
            "tv_power":  "/tv/on  /tv/off  /tv/status",
            "tv_audio":  "/tv/mute  /tv/volume/<n>",
            "tv_source": "/tv/source/<hdmi1|hdmi2|hdmi3|hdmi4|tv|av>",
            "tv_apps":   "/tv/app/<youtube|prime|spotify|appletv>  /tv/apps",
            "tv_play":   "/tv/play  /tv/pause  /tv/stop  /tv/ff  /tv/rewind  /tv/next  /tv/prev",
            "tv_nav":    "/tv/home  /tv/back  /tv/up  /tv/down  /tv/left  /tv/right  /tv/enter",
            "tv_raw":    "/tv/key/<KEY_CODE>",
            "lamp":      "/lamp/<endpoint>",
            "nfc":       "POST /nfc/scan  POST /nfc/register  GET /nfc/tags",
            "nfc_legacy": "GET /tag/<uid>  GET /tag/<uid>/<scene>  GET /tags",
            "presence":  "GET /presence  POST /presence  {state: home|away}",
            "spotify":   "/spotify/status  /spotify/play  /spotify/pause  /spotify/next  /spotify/prev  /spotify/volume/<n>  /spotify/shuffle/<on|off>  /spotify/repeat/<off|track|context>  /spotify/search/<q>  /spotify/devices  /spotify/auth",
            "tts":       "/tts/on  /tts/off  /tts/status",
            "announce":  "POST /api/announce  {text, device?}",
            "voice":     "POST /api/voice  (multipart: audio=<wav>)  → Whisper→Claude→dispatch",
            "shortcuts": f"http://{HUB_IP}:{PORT}/shortcuts",
        },
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if _MEM_OK:
        try:
            MEM.init()
        except Exception as _e:
            import logging as _lg; _lg.getLogger("hub").warning("memory.init() failed: %s", _e)
    if _SCHED_OK:
        try:
            SCHED.start()
        except Exception as _e:
            import logging as _lg; _lg.getLogger("hub").warning("scheduler.start() failed: %s", _e)
    print(f"Smart Home Hub  —  port {PORT}")
    print(f"TV: {TV.TV_IP}  Apps: {', '.join(TV.APPS)}  Sources: {', '.join(TV.SOURCES)}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
