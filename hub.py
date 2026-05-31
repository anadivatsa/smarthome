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
NFC tags:   GET /tag/<uid>             tap → executes scene
            GET /tag/<uid>/<scene>     register tag to scene
            GET /tags                  list all tags
"""

import json
import os
import threading
from pathlib import Path

import requests
from flask import Flask, jsonify

import tv as TV

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT        = int(os.getenv("HUB_PORT", "5001"))
LAMP_BASE   = os.getenv("LAMP_URL", "http://localhost:5000")
SCENES_FILE = Path(__file__).parent / "scenes.json"
TAGS_FILE   = Path(__file__).parent / "tags.json"

app = Flask(__name__)


def _load_scenes() -> dict:
    return json.loads(SCENES_FILE.read_text())


def _load_tags() -> dict:
    return json.loads(TAGS_FILE.read_text()).get("tags", {})


def _save_tag(uid: str, scene: str):
    data = json.loads(TAGS_FILE.read_text())
    data.setdefault("tags", {})[uid.upper()] = scene
    TAGS_FILE.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Lamp proxy
# ---------------------------------------------------------------------------

def _lamp(endpoint: str) -> tuple[bool, dict]:
    try:
        r = requests.get(f"{LAMP_BASE}/{endpoint.lstrip('/')}", timeout=8)
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

    def do_lamp():
        if lamp_ep:
            ok, data = _lamp(lamp_ep)
            results["lamp"] = data
            if not ok:
                errors.append(f"lamp: {data.get('error')}")

    def do_tv():
        if not tv_cfg:
            return
        action = tv_cfg.get("action")
        volume = tv_cfg.get("volume")
        app    = tv_cfg.get("app")

        if action == "off":
            ok, err = TV.tv_off()
            results["tv_power"] = {"ok": ok, "error": err}
            return

        if action == "on":
            # Bug fix: tv_on() is now state-aware — checks current power
            # state and uses WoL if fully off, KEY_POWER if standby, skips
            # if already on. Never blindly toggles.
            ok, err = TV.tv_on()
            results["tv_power"] = {"ok": ok, "error": err}
            if not ok:
                return

            # Bug fix: wait for WebSocket to be ready before sending
            # volume/app commands — TV needs a few seconds after boot.
            if app or volume:
                if not TV._wait_for_ws(timeout=15):
                    results["tv_ready"] = {"ok": False, "error": "WebSocket not ready after power-on"}
                    return
                results["tv_ready"] = {"ok": True}

        # Sequential: volume first, then app — order matters
        if volume:
            ok, err = TV.tv_set_volume(volume)
            results["tv_volume"] = {"ok": ok, "error": err}
        if app:
            ok, err = TV.tv_launch_app(app)
            results["tv_app"] = {"ok": ok, "app": app, "error": err}

    t_lamp = threading.Thread(target=do_lamp)
    t_tv   = threading.Thread(target=do_tv)
    t_lamp.start(); t_tv.start()
    t_lamp.join();  t_tv.join()

    return len(errors) == 0, {"scene": name, "results": results, "errors": errors}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(data: dict, extra: dict | None = None) -> tuple:
    payload = {**data, **(extra or {})}
    return jsonify(payload), (200 if data.get("ok", data.get("error") is None) else 500)


# ---------------------------------------------------------------------------
# Scene routes
# ---------------------------------------------------------------------------

@app.route("/scene/<name>")
def route_scene(name):
    ok, data = _run_scene(name)
    return jsonify(data), (200 if ok else 207)


@app.route("/scenes")
def route_scenes():
    scenes = _load_scenes()
    return jsonify({"scenes": list(scenes.keys()), "count": len(scenes)})


# ---------------------------------------------------------------------------
# TV — power
# ---------------------------------------------------------------------------

@app.route("/tv/status")
def route_tv_status():
    return jsonify(TV.tv_status())


@app.route("/tv/on")
def route_tv_on():
    ok, err = TV.tv_on()
    return jsonify({"ok": ok, "error": err})


@app.route("/tv/off")
def route_tv_off():
    ok, err = TV.tv_off()
    return jsonify({"ok": ok, "error": err})


# ---------------------------------------------------------------------------
# TV — audio
# ---------------------------------------------------------------------------

@app.route("/tv/mute")
def route_tv_mute():
    ok, err = TV.tv_mute()
    return jsonify({"ok": ok, "action": "mute_toggle", "error": err})


@app.route("/tv/volume/<int:delta>")
def route_tv_volume(delta):
    ok, err = TV.tv_set_volume(delta)
    return jsonify({"ok": ok, "delta": delta, "direction": "up" if delta > 0 else "down", "error": err})


# ---------------------------------------------------------------------------
# TV — source
# ---------------------------------------------------------------------------

@app.route("/tv/source/<name>")
def route_tv_source(name):
    ok, err = TV.tv_source(name)
    return jsonify({"ok": ok, "source": name, "error": err,
                    "available": list(TV.SOURCES.keys())})


# ---------------------------------------------------------------------------
# TV — apps
# ---------------------------------------------------------------------------

@app.route("/tv/app/<name>")
def route_tv_app(name):
    ok, err = TV.tv_launch_app(name)
    return jsonify({"ok": ok, "app": name,
                    "installed": list(TV.APPS.keys()), "error": err})


@app.route("/tv/apps")
def route_tv_apps():
    return jsonify({"installed": TV.APPS})


# ---------------------------------------------------------------------------
# TV — playback
# ---------------------------------------------------------------------------

@app.route("/tv/play")
def route_tv_play():    return jsonify({"ok": TV.tv_play()[0]})

@app.route("/tv/pause")
def route_tv_pause():   return jsonify({"ok": TV.tv_pause()[0]})

@app.route("/tv/stop")
def route_tv_stop():    return jsonify({"ok": TV.tv_stop()[0]})

@app.route("/tv/ff")
def route_tv_ff():      return jsonify({"ok": TV.tv_ff()[0]})

@app.route("/tv/rewind")
def route_tv_rewind():  return jsonify({"ok": TV.tv_rewind()[0]})

@app.route("/tv/next")
def route_tv_next():    return jsonify({"ok": TV.tv_next()[0]})

@app.route("/tv/prev")
def route_tv_prev():    return jsonify({"ok": TV.tv_prev()[0]})


# ---------------------------------------------------------------------------
# TV — navigation
# ---------------------------------------------------------------------------

@app.route("/tv/home")
def route_tv_home():    return jsonify({"ok": TV.tv_home()[0]})

@app.route("/tv/back")
def route_tv_back():    return jsonify({"ok": TV.tv_back()[0]})

@app.route("/tv/up")
def route_tv_up():      return jsonify({"ok": TV.tv_up()[0]})

@app.route("/tv/down")
def route_tv_down():    return jsonify({"ok": TV.tv_down()[0]})

@app.route("/tv/left")
def route_tv_left():    return jsonify({"ok": TV.tv_left()[0]})

@app.route("/tv/right")
def route_tv_right():   return jsonify({"ok": TV.tv_right()[0]})

@app.route("/tv/enter")
def route_tv_enter():   return jsonify({"ok": TV.tv_enter()[0]})


# ---------------------------------------------------------------------------
# TV — raw key (catch-all)
# ---------------------------------------------------------------------------

@app.route("/tv/key/<key>")
def route_tv_key(key):
    ok, err = TV.tv_key(key)
    return jsonify({"ok": ok, "key": key, "error": err})


# ---------------------------------------------------------------------------
# Lamp proxy
# ---------------------------------------------------------------------------

@app.route("/lamp/", defaults={"path": ""})
@app.route("/lamp/<path:path>")
def route_lamp(path):
    ok, data = _lamp(path)
    return jsonify(data), (200 if ok else 502)


# ---------------------------------------------------------------------------
# NFC tag routes
# ---------------------------------------------------------------------------

@app.route("/tag/<uid>")
def route_tag(uid):
    uid   = uid.upper().replace(":", "")
    tags  = _load_tags()
    scene = tags.get(uid)
    if scene:
        ok, data = _run_scene(scene)
        data["uid"] = uid
        return jsonify(data), (200 if ok else 207)
    return jsonify({
        "registered": False,
        "uid":   uid,
        "hint":  f"Register with GET /tag/{uid}/<scene>",
        "scenes": list(_load_scenes().keys()),
    }), 404


@app.route("/tag/<uid>/<scene>", methods=["GET", "POST"])
def route_tag_register(uid, scene):
    uid    = uid.upper().replace(":", "")
    scenes = _load_scenes()
    if scene not in scenes:
        return jsonify({"error": f"Unknown scene '{scene}'", "available": list(scenes)}), 400
    _save_tag(uid, scene)
    return jsonify({"registered": True, "uid": uid, "scene": scene})


@app.route("/tags")
def route_tags():
    return jsonify({"tags": _load_tags(), "scenes": list(_load_scenes().keys())})


# ---------------------------------------------------------------------------
# Health / index
# ---------------------------------------------------------------------------

@app.route("/")
def route_index():
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
            "nfc":       "/tag/<uid>  /tag/<uid>/<scene>  /tags",
        },
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Smart Home Hub  —  port {PORT}")
    print(f"TV: {TV.TV_IP}  Apps: {', '.join(TV.APPS)}  Sources: {', '.join(TV.SOURCES)}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
