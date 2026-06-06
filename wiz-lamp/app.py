#!/usr/bin/env python3
"""
WiZ Smart Lamp Controller — Flask HTTP API

Static scenes:
  GET /on              Full brightness white
  GET /off             Turn off (cancels any effect/transition)
  GET /focus           Cool white, 100%      (6500 K)
  GET /movie           Warm white, 30%       (2700 K)
  GET /sleep           Very warm, 10%        (2200 K)
  GET /relax           Soft amber, 40%       (2400 K)
  GET /reading         Neutral white, 80%    (4000 K)
  GET /romance         Deep warm red, 15%
  GET /dinner          Candlelight, 50%      (2500 K)
  GET /morning         Cool daylight, 70%    (5000 K)
  GET /gaming          Vivid blue-white, 60%
  GET /brightness/<n>  Set brightness 0–100%
  GET /disco           Rainbow sweep with brightness waves (light show)

Looping effects (run until /off or another command):
  GET /blink           Fast on/off flash
  GET /pulse           Slow breathing dim↔bright
  GET /party           Random colour cycling
  GET /alert           Red SOS flash
  GET /strobe          Fast white strobe
  GET /candle          Warm candle flicker
  GET /campfire        Intense fire flicker
  GET /aurora          Slow northern-lights colour drift

Transitions (run once — lamp settles at final state or turns off):
  GET /sunset          Golden → deep red → off          (~5.5 min)
  GET /sunrise         Deep red → orange → warm white   (~5 min)
  GET /wake            Dim warm → full daylight          (~90 s)
  GET /bedtime         Medium → warm dim sleep level     (~2 min)
  GET /fade            Current → off                     (~30 s)
  GET /goodnight       Long gentle fade to off           (~5.5 min)

  GET /status          Current lamp state + effect info
"""

import asyncio
import colorsys
import math
import os
import random
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify

load_dotenv(Path(__file__).parent / "config.env")

from pywizlight import wizlight, PilotBuilder  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LAMP_IP = os.getenv("LAMP_IP", "").strip()
PORT = int(os.getenv("PORT", "5000"))

if not LAMP_IP:
    sys.exit(
        "ERROR: LAMP_IP is not set in config.env.\n"
        "Run  python discover_lamp.py  to find your lamp, then edit config.env."
    )

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine in a fresh event loop — safe from any thread."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _send(pilot=None, turn_off=False):
    bulb = wizlight(LAMP_IP)
    try:
        if turn_off:
            await bulb.turn_off()
        else:
            await bulb.turn_on(pilot or PilotBuilder())
    finally:
        try:
            await bulb.async_close()
        except Exception:
            pass


async def _get_state():
    bulb = wizlight(LAMP_IP)
    try:
        return await bulb.updateState()
    finally:
        try:
            await bulb.async_close()
        except Exception:
            pass


_last_off_sent = 0.0   # epoch time of the most recent Pi-initiated turn_off
_last_off_lock = threading.Lock()

_WATCHDOG_INTERVAL = 3.0   # seconds between lamp polls
_YIELD_GRACE       = 2.0   # seconds after our own off before we trust it


def send(pilot=None, turn_off=False):
    global _last_off_sent
    try:
        _run(_send(pilot=pilot, turn_off=turn_off))
        if turn_off:
            with _last_off_lock:
                _last_off_sent = time.time()
        return True, None
    except Exception as exc:
        return False, str(exc)


def _watchdog(stop):
    """Stop the effect and turn the lamp off if another device takes control."""
    while not stop.wait(_WATCHDOG_INTERVAL):
        try:
            state = _run(_get_state())
            if state is None:
                continue
            if not state.get_state():
                with _last_off_lock:
                    age = time.time() - _last_off_sent
                if age > _YIELD_GRACE:
                    stop.set()
                    _run(_send(turn_off=True))
                    return
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Effect engine  (background thread, cancelled by any new command)
# ---------------------------------------------------------------------------

_effect_lock = threading.Lock()
_stop_event = threading.Event()
_effect_thread = None
_current_effect = None   # name of the running effect/transition


def _stop_effect():
    global _effect_thread, _current_effect
    _stop_event.set()
    if _effect_thread and _effect_thread.is_alive():
        _effect_thread.join(timeout=3)
    _effect_thread = None
    _current_effect = None


def _start_effect(name, fn):
    global _effect_thread, _stop_event, _current_effect
    with _effect_lock:
        _stop_effect()
        _stop_event = threading.Event()
        _current_effect = name
        t = threading.Thread(target=fn, args=(_stop_event,), daemon=True)
        t.start()
        _effect_thread = t
        threading.Thread(target=_watchdog, args=(_stop_event,), daemon=True).start()


def _stop_and_send(pilot=None, turn_off=False):
    with _effect_lock:
        _stop_effect()
    return send(pilot=pilot, turn_off=turn_off)


# ---------------------------------------------------------------------------
# Transition engine
# ---------------------------------------------------------------------------

STEPS_PER_SEC = 4   # lamp UDP commands per second during transitions


def _lerp(a, b, t):
    return a + (b - a) * t


def _run_rgb_transition(keyframes, stop):
    """
    Smoothly interpolate through RGB keyframes.
    keyframes: list of (duration_s, brightness 0-255, r, g, b)
    The first entry is the starting state (duration ignored).
    Only sends a command when the computed values actually change.
    """
    prev = None
    for i in range(1, len(keyframes)):
        dur, tb, tr, tg, tbl = keyframes[i]
        _,   sb, sr, sg, sbl = keyframes[i - 1]
        steps = max(1, int(dur * STEPS_PER_SEC))
        for step in range(steps):
            if stop.is_set():
                return
            t  = step / steps
            b  = max(0, min(255, round(_lerp(sb,  tb,  t))))
            r  = max(0, min(255, round(_lerp(sr,  tr,  t))))
            g  = max(0, min(255, round(_lerp(sg,  tg,  t))))
            bl = max(0, min(255, round(_lerp(sbl, tbl, t))))
            curr = (b, r, g, bl)
            if curr != prev:
                if b == 0:
                    _run(_send(turn_off=True))
                else:
                    _run(_send(PilotBuilder(rgb=(r, g, bl), brightness=b)))
                prev = curr
            stop.wait(1.0 / STEPS_PER_SEC)
    # apply final keyframe
    _, tb, tr, tg, tbl = keyframes[-1]
    if tb == 0:
        _run(_send(turn_off=True))


def _run_ct_transition(keyframes, stop):
    """
    Smoothly interpolate through colour-temperature keyframes.
    keyframes: list of (duration_s, brightness 0-255, colortemp K)
    """
    prev = None
    for i in range(1, len(keyframes)):
        dur, tb, tct = keyframes[i]
        _,   sb, sct = keyframes[i - 1]
        steps = max(1, int(dur * STEPS_PER_SEC))
        for step in range(steps):
            if stop.is_set():
                return
            t  = step / steps
            b  = max(0,    min(255,  round(_lerp(sb,  tb,  t))))
            ct = max(2200, min(6500, round(_lerp(sct, tct, t))))
            curr = (b, ct)
            if curr != prev:
                if b == 0:
                    _run(_send(turn_off=True))
                else:
                    _run(_send(PilotBuilder(colortemp=ct, brightness=b)))
                prev = curr
            stop.wait(1.0 / STEPS_PER_SEC)
    _, tb, _ = keyframes[-1]
    if tb == 0:
        _run(_send(turn_off=True))


# ---------------------------------------------------------------------------
# Keyframe data
# ---------------------------------------------------------------------------

# Sunset: warm golden → deep red → off  (~5.5 min total)
_KF_SUNSET = [
    #  dur   bright   r    g    b
    (  0,   180,   255, 160,  40),   # golden hour
    ( 60,   140,   255, 120,  20),   # deeper orange
    ( 60,    90,   255,  70,  10),   # red-orange
    ( 60,    40,   220,  30,   5),   # deep red
    ( 60,    10,   180,  10,   0),   # last ember
    ( 30,     0,     0,   0,   0),   # out
]

# Sunrise: deep red → orange → golden → warm white  (~5 min)
_KF_SUNRISE = [
    (  0,    5,   120,  10,   0),
    ( 60,   20,   200,  40,   5),
    ( 60,   60,   255,  80,  10),
    ( 60,  110,   255, 140,  30),
    ( 60,  170,   255, 180,  80),
    ( 30,  230,   255, 215, 160),
    ( 30,  255,   255, 235, 210),
]

# Wake: energising ramp from dim warm to full daylight  (~90 s, CT)
_KF_WAKE = [
    #  dur   bright  colortemp K
    (  0,   30,  2200),
    ( 20,  100,  2700),
    ( 25,  180,  3500),
    ( 25,  230,  5000),
    ( 20,  255,  6500),
]

# Bedtime: medium → warm dim  (~2 min, CT) — lamp stays on at sleep level
_KF_BEDTIME = [
    (  0,  200,  4000),
    ( 40,  150,  3000),
    ( 40,   80,  2500),
    ( 40,   25,  2200),
]

# Fade: smooth fade to off  (~30 s, CT)
_KF_FADE = [
    (  0,  200,  3000),
    ( 22,   15,  2200),
    (  8,    0,  2200),
]

# Goodnight: long peaceful fade to off  (~5.5 min, CT)
_KF_GOODNIGHT = [
    (  0,  200,  4000),
    ( 90,  120,  2700),
    ( 90,   50,  2200),
    ( 90,   15,  2200),
    ( 60,    0,  2200),
]

# Aurora: northern-lights colour drift  (RGB, loops ~48 s per cycle)
_KF_AURORA = [
    (  0,  160,   0, 200,  80),   # green-teal
    ( 12,  140,  30, 120, 255),   # indigo-blue
    ( 12,  160, 120,  30, 220),   # violet
    ( 12,  150,   0, 180, 200),   # cyan
    ( 12,  160,   0, 200,  80),   # back to green-teal (seamless loop)
]


# ---------------------------------------------------------------------------
# Looping effect functions
# ---------------------------------------------------------------------------

def _effect_blink(stop):
    while not stop.is_set():
        _run(_send(PilotBuilder(brightness=255, colortemp=4000)))
        if stop.wait(0.4):
            break
        _run(_send(turn_off=True))
        stop.wait(0.4)


def _effect_pulse(stop):
    steps = list(range(20, 256, 10)) + list(range(255, 19, -10))
    i = 0
    while not stop.is_set():
        _run(_send(PilotBuilder(brightness=steps[i % len(steps)], colortemp=3000)))
        i += 1
        stop.wait(0.07)


def _effect_party(stop):
    colours = [
        (255, 0, 0), (0, 255, 0), (0, 0, 255),
        (255, 255, 0), (255, 0, 255), (0, 255, 255),
        (255, 128, 0), (128, 0, 255), (0, 255, 128),
    ]
    while not stop.is_set():
        r, g, b = random.choice(colours)
        _run(_send(PilotBuilder(rgb=(r, g, b), brightness=220)))
        stop.wait(random.uniform(0.4, 0.9))


def _effect_alert(stop):
    short, long_, gap = 0.15, 0.45, 0.15
    pattern = [short] * 3 + [long_] * 3 + [short] * 3
    while not stop.is_set():
        for dur in pattern:
            if stop.is_set():
                return
            _run(_send(PilotBuilder(rgb=(255, 0, 0), brightness=255)))
            if stop.wait(dur):
                return
            _run(_send(turn_off=True))
            if stop.wait(gap):
                return
        stop.wait(0.8)


def _effect_strobe(stop):
    while not stop.is_set():
        _run(_send(PilotBuilder(brightness=255, colortemp=6500)))
        if stop.wait(0.08):
            break
        _run(_send(turn_off=True))
        stop.wait(0.08)


def _effect_candle(stop):
    while not stop.is_set():
        _run(_send(PilotBuilder(
            rgb=(random.randint(220, 255), random.randint(80, 130), random.randint(0, 20)),
            brightness=random.randint(60, 160),
        )))
        stop.wait(random.uniform(0.05, 0.18))


def _effect_campfire(stop):
    """Bigger, wilder flicker than candle."""
    while not stop.is_set():
        _run(_send(PilotBuilder(
            rgb=(random.randint(200, 255), random.randint(50, 120), random.randint(0, 15)),
            brightness=random.randint(40, 220),
        )))
        stop.wait(random.uniform(0.04, 0.14))


def _effect_aurora(stop):
    while not stop.is_set():
        _run_rgb_transition(_KF_AURORA, stop)


def _effect_disco(stop):
    """Rainbow hue sweep + sine brightness wave + occasional white flash."""
    FRAME      = 0.025   # ~40 fps
    HUE_RATE   = 15.0    # degrees of hue advanced per frame
    WAVE_FREQ  = 0.45    # brightness wave frequency (cycles/frame)
    FLASH_PROB = 0.04    # chance per frame of a white flash

    hue   = 0.0
    phase = 0.0
    while not stop.is_set():
        # white flash burst
        if random.random() < FLASH_PROB:
            _run(_send(PilotBuilder(brightness=255, colortemp=6500)))
            if stop.wait(0.06):
                break
            _run(_send(PilotBuilder(brightness=255, colortemp=6500)))
            if stop.wait(0.06):
                break

        # sine-wave brightness: oscillates between 30 and 255
        brightness = int(142 + 113 * math.sin(phase))

        r, g, b = colorsys.hsv_to_rgb(hue / 360.0, 1.0, 1.0)
        _run(_send(PilotBuilder(
            rgb=(int(r * 255), int(g * 255), int(b * 255)),
            brightness=brightness,
        )))

        hue   = (hue + HUE_RATE) % 360.0
        phase = (phase + 2 * math.pi * WAVE_FREQ) % (2 * math.pi)
        stop.wait(FRAME)


# ---------------------------------------------------------------------------
# Transition functions  (run once, settle at final state)
# ---------------------------------------------------------------------------

def _effect_sunset(stop):
    _run_rgb_transition(_KF_SUNSET, stop)


def _effect_sunrise(stop):
    _run_rgb_transition(_KF_SUNRISE, stop)


def _effect_wake(stop):
    _run_ct_transition(_KF_WAKE, stop)


def _effect_bedtime(stop):
    _run_ct_transition(_KF_BEDTIME, stop)


def _effect_fade(stop):
    _run_ct_transition(_KF_FADE, stop)


def _effect_goodnight(stop):
    _run_ct_transition(_KF_GOODNIGHT, stop)


# ---------------------------------------------------------------------------
# Static scene routes
# ---------------------------------------------------------------------------

@app.route("/on")
def route_on():
    ok, err = _stop_and_send(PilotBuilder(brightness=255))
    return jsonify({"status": "on"}) if ok else (jsonify({"status": "error", "message": err}), 500)


@app.route("/off")
def route_off():
    ok, err = _stop_and_send(turn_off=True)
    return jsonify({"status": "off"}) if ok else (jsonify({"status": "error", "message": err}), 500)


@app.route("/focus")
def route_focus():
    ok, err = _stop_and_send(PilotBuilder(colortemp=6500, brightness=255))
    return jsonify({"status": "focus", "colortemp_k": 6500, "brightness_pct": 100}) if ok else (jsonify({"status": "error", "message": err}), 500)


@app.route("/movie")
def route_movie():
    ok, err = _stop_and_send(PilotBuilder(colortemp=2700, brightness=77))
    return jsonify({"status": "movie", "colortemp_k": 2700, "brightness_pct": 30}) if ok else (jsonify({"status": "error", "message": err}), 500)


@app.route("/sleep")
def route_sleep():
    ok, err = _stop_and_send(PilotBuilder(colortemp=2200, brightness=25))
    return jsonify({"status": "sleep", "colortemp_k": 2200, "brightness_pct": 10}) if ok else (jsonify({"status": "error", "message": err}), 500)


@app.route("/relax")
def route_relax():
    ok, err = _stop_and_send(PilotBuilder(colortemp=2400, brightness=102))
    return jsonify({"status": "relax", "colortemp_k": 2400, "brightness_pct": 40}) if ok else (jsonify({"status": "error", "message": err}), 500)


@app.route("/reading")
def route_reading():
    ok, err = _stop_and_send(PilotBuilder(colortemp=4000, brightness=204))
    return jsonify({"status": "reading", "colortemp_k": 4000, "brightness_pct": 80}) if ok else (jsonify({"status": "error", "message": err}), 500)


@app.route("/romance")
def route_romance():
    ok, err = _stop_and_send(PilotBuilder(rgb=(220, 30, 10), brightness=38))
    return jsonify({"status": "romance", "brightness_pct": 15}) if ok else (jsonify({"status": "error", "message": err}), 500)


@app.route("/dinner")
def route_dinner():
    ok, err = _stop_and_send(PilotBuilder(colortemp=2500, brightness=128))
    return jsonify({"status": "dinner", "colortemp_k": 2500, "brightness_pct": 50}) if ok else (jsonify({"status": "error", "message": err}), 500)


@app.route("/morning")
def route_morning():
    ok, err = _stop_and_send(PilotBuilder(colortemp=5000, brightness=178))
    return jsonify({"status": "morning", "colortemp_k": 5000, "brightness_pct": 70}) if ok else (jsonify({"status": "error", "message": err}), 500)


@app.route("/gaming")
def route_gaming():
    ok, err = _stop_and_send(PilotBuilder(rgb=(60, 120, 255), brightness=153))
    return jsonify({"status": "gaming", "brightness_pct": 60}) if ok else (jsonify({"status": "error", "message": err}), 500)


@app.route("/blue")
def route_blue():
    ok, err = _stop_and_send(PilotBuilder(rgb=(0, 0, 255), brightness=204))
    return jsonify({"status": "blue", "brightness_pct": 80}) if ok else (jsonify({"status": "error", "message": err}), 500)


@app.route("/brightness/<int:pct>")
def route_brightness(pct):
    pct = max(0, min(100, pct))
    if pct == 0:
        ok, err = _stop_and_send(turn_off=True)
        return jsonify({"status": "off"}) if ok else (jsonify({"status": "error", "message": err}), 500)
    ok, err = _stop_and_send(PilotBuilder(brightness=round(pct * 255 / 100)))
    return jsonify({"status": "brightness", "brightness_pct": pct}) if ok else (jsonify({"status": "error", "message": err}), 500)


# ---------------------------------------------------------------------------
# Looping effect routes
# ---------------------------------------------------------------------------

@app.route("/blink")
def route_blink():
    _start_effect("blink", _effect_blink)
    return jsonify({"status": "blink", "note": "GET /off to stop"})


@app.route("/pulse")
def route_pulse():
    _start_effect("pulse", _effect_pulse)
    return jsonify({"status": "pulse", "note": "GET /off to stop"})


@app.route("/party")
def route_party():
    _start_effect("party", _effect_party)
    return jsonify({"status": "party", "note": "GET /off to stop"})


@app.route("/alert")
def route_alert():
    _start_effect("alert", _effect_alert)
    return jsonify({"status": "alert", "note": "GET /off to stop"})


@app.route("/strobe")
def route_strobe():
    _start_effect("strobe", _effect_strobe)
    return jsonify({"status": "strobe", "note": "GET /off to stop"})


@app.route("/candle")
def route_candle():
    _start_effect("candle", _effect_candle)
    return jsonify({"status": "candle", "note": "GET /off to stop"})


@app.route("/campfire")
def route_campfire():
    _start_effect("campfire", _effect_campfire)
    return jsonify({"status": "campfire", "note": "GET /off to stop"})


@app.route("/aurora")
def route_aurora():
    _start_effect("aurora", _effect_aurora)
    return jsonify({"status": "aurora", "note": "GET /off to stop"})


@app.route("/disco")
def route_disco():
    _start_effect("disco", _effect_disco)
    return jsonify({"status": "disco", "note": "GET /off to stop"})


# ---------------------------------------------------------------------------
# Transition routes
# ---------------------------------------------------------------------------

@app.route("/sunset")
def route_sunset():
    _start_effect("sunset", _effect_sunset)
    return jsonify({"status": "sunset", "duration_min": 5.5, "note": "Fades to off — GET /off to cancel"})


@app.route("/sunrise")
def route_sunrise():
    _start_effect("sunrise", _effect_sunrise)
    return jsonify({"status": "sunrise", "duration_min": 5, "note": "Ramps to warm white — GET /off to cancel"})


@app.route("/wake")
def route_wake():
    _start_effect("wake", _effect_wake)
    return jsonify({"status": "wake", "duration_sec": 90, "note": "Ramps to full daylight"})


@app.route("/bedtime")
def route_bedtime():
    _start_effect("bedtime", _effect_bedtime)
    return jsonify({"status": "bedtime", "duration_min": 2, "note": "Dims to sleep level"})


@app.route("/fade")
def route_fade():
    _start_effect("fade", _effect_fade)
    return jsonify({"status": "fade", "duration_sec": 30, "note": "Fades to off"})


@app.route("/goodnight")
def route_goodnight():
    _start_effect("goodnight", _effect_goodnight)
    return jsonify({"status": "goodnight", "duration_min": 5.5, "note": "Long peaceful fade to off"})


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@app.route("/status")
def route_status():
    try:
        state = _run(_get_state())
        if state is None:
            return jsonify({"status": "error", "message": "No response from lamp"}), 503
        running = _effect_thread is not None and _effect_thread.is_alive()
        return jsonify({
            "on": state.get_state(),
            "brightness_pct": state.get_brightness(),
            "colortemp_k": state.get_colortemp(),
            "rgb": state.get_rgb(),
            "effect": _current_effect if running else None,
        })
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"WiZ Lamp Controller  —  lamp at {LAMP_IP}  —  listening on :{PORT}")
    print("Static:      /on /off /focus /movie /sleep /relax /reading /romance /dinner /morning /gaming /brightness/<0-100>")
    print("Effects:     /blink /pulse /party /alert /strobe /candle /campfire /aurora /disco")
    print("Transitions: /sunset /sunrise /wake /bedtime /fade /goodnight")
    app.run(host="127.0.0.1", port=PORT, threaded=True)
