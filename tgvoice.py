#!/usr/bin/env python3
"""
Telegram voice-message handler for Neo smart home.

Flow: voice message → download OGG → convert to WAV → Whisper → Claude → hub → reply
Reuses transcribe() / resolve_intent() / dispatch() from voice.py.
"""

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Env vars injected by systemd EnvironmentFile= (voice.env + notifier.env)
BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")   # only respond to this chat
HUB_URL      = os.getenv("HUB_URL", "http://localhost:5001")
MODEL        = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
SCENES_FILE  = Path(__file__).parent / "scenes.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [tgvoice] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tgvoice")

# ---------------------------------------------------------------------------
# Whisper (lazy-loaded, serialised, auto-unloaded after 10 min idle)
# ---------------------------------------------------------------------------

import threading
import whisper

WHISPER_IDLE_TTL = 600  # seconds before unloading to free ~800 MB RAM

_whisper_lock  = threading.Semaphore(1)
_whisper_model = None
_whisper_timer: threading.Timer | None = None


def _unload_whisper():
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is not None:
            _whisper_model = None
            log.info("Whisper unloaded after %ds idle.", WHISPER_IDLE_TTL)


def _reset_idle_timer():
    global _whisper_timer
    if _whisper_timer is not None:
        _whisper_timer.cancel()
    _whisper_timer = threading.Timer(WHISPER_IDLE_TTL, _unload_whisper)
    _whisper_timer.daemon = True
    _whisper_timer.start()


def _load_whisper():
    global _whisper_model
    if _whisper_model is None:
        log.info("Loading Whisper '%s' model…", WHISPER_MODEL)
        _whisper_model = whisper.load_model(WHISPER_MODEL)
        log.info("Whisper ready.")
    _reset_idle_timer()
    return _whisper_model


def transcribe(wav_path: str) -> str:
    with _whisper_lock:
        result = _load_whisper().transcribe(wav_path, language="en", fp16=False)
        return result["text"].strip()


# ---------------------------------------------------------------------------
# Claude intent (same system prompt as voice.py)
# ---------------------------------------------------------------------------

import anthropic

_claude = None


def _get_claude():
    global _claude
    if _claude is None:
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set in voice.env")
        _claude = anthropic.Anthropic(api_key=key)
    return _claude


def _build_system_prompt() -> str:
    scenes = json.loads(SCENES_FILE.read_text())

    def _summary(name, cfg):
        lamp = cfg.get("lamp", "—")
        tv   = cfg.get("tv", {})
        parts = [f"lamp={lamp}", f"tv={tv.get('action','—')}"]
        if tv.get("app"):     parts.append(f"app={tv['app']}")
        if cfg.get("spotify"): parts.append("spotify=play")
        if cfg.get("presence"): parts.append(f"presence={cfg['presence']}")
        return ", ".join(parts)

    scene_lines = "\n".join(
        f"  /scene/{n}  ({_summary(n, c)})" for n, c in scenes.items()
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
Audio:       mute   volume/<n>  (positive=louder, negative=quieter)
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


_system_prompt = None


def resolve_intent(transcript: str) -> list:
    global _system_prompt
    if _system_prompt is None:
        _system_prompt = _build_system_prompt()
    raw = ""
    try:
        msg = _get_claude().messages.create(
            model=MODEL,
            max_tokens=256,
            system=_system_prompt,
            messages=[{"role": "user", "content": transcript}],
        )
        raw = msg.content[0].text.strip()
        # Strip markdown code fences if Claude wraps the JSON
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        parsed = json.loads(raw)
        return [parsed] if isinstance(parsed, dict) else [a for a in parsed if isinstance(a, dict)]
    except json.JSONDecodeError:
        log.warning("Claude non-JSON: %.120s", raw)
        return [{"action": None, "reason": "JSON parse error"}]
    except Exception as exc:
        log.error("Claude API error: %s", exc)
        return [{"action": None, "reason": str(exc)}]


# ---------------------------------------------------------------------------
# Hub dispatcher
# ---------------------------------------------------------------------------

def _hub_headers() -> dict:
    key = os.getenv("NEO_API_KEY", "").strip()
    return {"X-Neo-Key": key} if key else {}


def dispatch(actions: list) -> list[str]:
    """Fire hub endpoints; return human-readable result lines."""
    results = []
    for item in actions:
        action = item.get("action")
        reason = item.get("reason", "")
        if not action:
            log.info("No-op — %s", reason)
            results.append(f"No action — {reason}")
            continue
        url = f"{HUB_URL}{action}"
        log.info("→ %-30s  %s", action, reason)
        try:
            r = requests.get(url, headers=_hub_headers(), timeout=60)
            results.append(f"✓ {action}")
            log.debug("   HTTP %d", r.status_code)
        except Exception as exc:
            results.append(f"✗ {action} ({exc})")
            log.error("   dispatch failed: %s", exc)
    return results


# ---------------------------------------------------------------------------
# /status helpers
# ---------------------------------------------------------------------------

def _fmt_lamp(d: dict) -> str:
    if not d.get("on"):
        return "off"
    parts = []
    if d.get("colortemp_k"):
        parts.append(f"{d['colortemp_k']}K")
    if d.get("brightness_pct") is not None:
        parts.append(f"{d['brightness_pct']}%")
    if d.get("effect"):
        parts.append(d["effect"])
    return "on · " + " · ".join(parts) if parts else "on"


def _fmt_tv(d: dict) -> str:
    return d.get("state", "unknown")


def _fmt_spotify(d: dict) -> str:
    if not d.get("is_playing"):
        return "not playing"
    track  = d.get("track", "")
    artist = d.get("artist", "")
    return f"▶ {track} — {artist}" if track else "playing"


def _fmt_presence(d: dict) -> str:
    return d.get("state", "unknown")


# ---------------------------------------------------------------------------
# Telegram handler
# ---------------------------------------------------------------------------

def _is_allowed(msg) -> bool:
    return not ALLOWED_CHAT or str(msg.chat_id) == ALLOWED_CHAT


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not _is_allowed(msg):
        return

    lines = ["*Neo Status*"]

    services = ["hub", "wiz-lamp", "voice", "tgvoice", "bayern-notifier"]
    for svc in services:
        r = subprocess.run(
            ["systemctl", "is-active", f"{svc}.service"],
            capture_output=True, text=True,
        )
        icon = "✅" if r.stdout.strip() == "active" else "❌"
        lines.append(f"{icon} {svc}")

    lines.append("")
    for path, label, fmt in [
        ("/lamp/status",    "Lamp",     _fmt_lamp),
        ("/tv/status",      "TV",       _fmt_tv),
        ("/spotify/status", "Spotify",  _fmt_spotify),
        ("/presence",       "Presence", _fmt_presence),
    ]:
        try:
            r = requests.get(f"{HUB_URL}{path}", headers=_hub_headers(), timeout=5)
            lines.append(f"{label}: {fmt(r.json())}")
        except Exception as exc:
            lines.append(f"{label}: ⚠️ {exc}")

    await msg.reply_text("\n".join(lines), parse_mode="Markdown")


async def send_tip(context: ContextTypes.DEFAULT_TYPE):
    if not ALLOWED_CHAT:
        return
    try:
        scenes = json.loads(SCENES_FILE.read_text())
        scene_list = ", ".join(scenes.keys())
        result = _get_claude().messages.create(
            model=MODEL,
            max_tokens=150,
            system=(
                "You are a smart home assistant for Neo, a Raspberry Pi hub. "
                "Generate ONE practical, interesting tip about the system. "
                "2-3 sentences max. Start with a relevant emoji. Plain text only."
            ),
            messages=[{"role": "user", "content": (
                f"Neo capabilities: scenes ({scene_list}), local mic VAD voice control, "
                "Telegram text+voice commands, Spotify playback + beat sync, "
                "Samsung TV control, WiZ lamp with effects (aurora, disco, candle, campfire, strobe, pulse, party), "
                "transitions (sunrise, sunset, goodnight, wake, bedtime, fade), "
                "NFC tags, presence detection. "
                "Give one tip highlighting something useful or underused."
            )}],
        )
        tip = result.content[0].text.strip()
        await context.bot.send_message(chat_id=int(ALLOWED_CHAT), text=tip)
        log.info("Tip sent: %.80s", tip)
    except Exception as exc:
        log.error("Failed to send tip: %s", exc)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not _is_allowed(msg):
        return

    voice = msg.voice or msg.audio
    if not voice:
        return

    await msg.reply_text("🎙 Transcribing…")

    with tempfile.TemporaryDirectory() as tmp:
        ogg_path = os.path.join(tmp, "voice.ogg")
        wav_path = os.path.join(tmp, "voice.wav")

        tg_file = await context.bot.get_file(voice.file_id)
        await tg_file.download_to_drive(ogg_path)

        subprocess.run(
            ["ffmpeg", "-y", "-i", ogg_path, "-ar", "16000", "-ac", "1", wav_path],
            check=True, capture_output=True,
        )

        try:
            transcript = transcribe(wav_path)
        except Exception as exc:
            await msg.reply_text(f"Transcription failed: {exc}")
            return

    if not transcript:
        await msg.reply_text("Could not understand the audio.")
        return

    log.info('Heard: "%s"', transcript)
    actions = resolve_intent(transcript)
    results = dispatch(actions)
    await msg.reply_text(f'🗣 "{transcript}"\n' + "\n".join(results))


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not _is_allowed(msg):
        return

    transcript = msg.text.strip()
    if not transcript:
        return

    log.info('Text: "%s"', transcript)
    actions = resolve_intent(transcript)
    results = dispatch(actions)
    await msg.reply_text(f'💬 "{transcript}"\n' + "\n".join(results))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set in notifier.env")
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        raise SystemExit("ANTHROPIC_API_KEY not set in voice.env")

    # Warm up Whisper at startup
    _load_whisper()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("status", handle_status))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    if app.job_queue and ALLOWED_CHAT:
        app.job_queue.run_repeating(send_tip, interval=7200, first=60)
        log.info("Tip scheduler active — every 2h, first in 60s")

    log.info("Bot started — polling for voice messages (chat_id=%s)", ALLOWED_CHAT or "any")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
