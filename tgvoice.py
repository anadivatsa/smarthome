#!/usr/bin/env python3
"""
Telegram voice-message handler for Neo smart home.

Flow: voice message → download OGG → convert to WAV → Whisper → Claude → hub → reply
Reuses transcribe() / resolve_intent() / dispatch() from voice.py.
"""

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

import memory
import backup
import scene_rag

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

Single home-control action:
{{"action": "/scene/movie", "reason": "user wants to watch a movie"}}

Multiple actions (executed in order):
[
  {{"action": "/spotify/pause", "reason": "silence music first"}},
  {{"action": "/scene/focus",   "reason": "activate focus mode"}}
]

Self-knowledge query (user asks about Neo's own system):
{{"action": "ask_neo", "reason": "brief description of what they're asking"}}

No-op (unclear input, filler words, background noise):
{{"action": null, "reason": "command unclear or no matching action"}}

## ASK_NEO — questions about Neo itself
Use {{"action": "ask_neo"}} when the user asks about:
- Neo's architecture, services, design decisions, or why something was built a certain way
- Configuration files or environment variables ("what's in voice.env?")
- History or past decisions ("why did we retire the wakeword service?")
- Past diary entries ("what did Neo write in its diary?")
- How a specific feature works internally

Examples → ask_neo (NOT null, NOT a home-control endpoint):
  "why did we retire the wakeword service"
  "what's in voice.env"
  "what did Neo write in its diary recently"
  "how does beat sync work"
  "tell me about the BT presence detection"
  "what scenes do we have and what do they do"  ← system knowledge, not activation

## Matching rules
- Prefer /scene/* over separate /lamp + /tv calls — scenes handle both together
- Volume: "louder" → /tv/volume/10,  "a bit louder" → /tv/volume/5,  "quieter" → /tv/volume/-10
- Brightness: "dim it" → /lamp/brightness/20,  "brighter" → /lamp/brightness/80
- Genuine self-knowledge questions → ask_neo (never null for these)
- Filler words, coughs, background noise → action: null
- Never output anything other than valid JSON"""


_system_prompt = None


def resolve_intent(transcript: str,
                   history: list | None = None,
                   mem_context: list | None = None) -> list:
    global _system_prompt
    if _system_prompt is None:
        _system_prompt = _build_system_prompt()

    system = _system_prompt
    if mem_context:
        lines = "\n".join(f"- {m['content'][:300]}" for m in mem_context)
        system += f"\n\nRelevant context from memory:\n{lines}"

    messages = list(history or []) + [{"role": "user", "content": transcript}]
    raw = ""
    try:
        msg = _get_claude().messages.create(
            model=MODEL,
            max_tokens=256,
            system=system,
            messages=messages,
        )
        raw = msg.content[0].text.strip()
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
            # Log scene activations to memory
            if action.startswith("/scene/"):
                scene_name = action.split("/scene/", 1)[1].split("?")[0]
                try:
                    memory.store_scene_event(scene_name, "telegram")
                except Exception:
                    pass
        except Exception as exc:
            results.append(f"✗ {action} ({exc})")
            log.error("   dispatch failed: %s", exc)
    return results


def _rag_fallback(transcript: str) -> tuple[list[str], str | None]:
    """Try scene_rag when Claude returns no-op. Returns (result_lines, natural_reply)."""
    try:
        result = scene_rag.run(transcript)
        scene = result.get("scene")
        if not scene:
            return [], None
        log.info("RAG fallback → /scene/%s", scene)
        lines = dispatch([{"action": f"/scene/{scene}", "reason": "semantic match"}])
        return lines, result.get("reply")
    except Exception as exc:
        log.warning("RAG fallback failed: %s", exc)
        return [], None


# ---------------------------------------------------------------------------
# ASK_NEO — self-knowledge RAG answer
# ---------------------------------------------------------------------------

_FTS_STRIP    = re.compile(r"[^\w\s]")
_FTS_STOPWORDS = frozenset({
    "what", "why", "how", "when", "where", "which", "who",
    "did", "does", "do", "is", "are", "was", "were", "will", "can",
    "could", "would", "should", "have", "has", "had", "been", "be",
    "the", "a", "an", "in", "of", "to", "for", "at", "by", "with",
    "that", "this", "it", "its", "we", "our", "you", "me", "my",
    "tell", "show", "get", "give", "recently", "lately", "s",
})


def _sanitize_fts(question: str) -> str:
    """Strip punctuation and stop words; return a clean FTS5 query string."""
    words = _FTS_STRIP.sub(" ", question.lower()).split()
    kept  = [w for w in words if w not in _FTS_STOPWORDS and len(w) > 2]
    return " ".join(kept) if kept else (words[0] if words else "neo")


def _neo_rag_answer(question: str) -> str:
    """Search RAG knowledge base (docs/scene_config/env_keys/diary) and answer via Claude."""
    fts_q = _sanitize_fts(question)
    log.info("ASK_NEO FTS query: %r", fts_q)

    conn       = memory._conn()
    seen_srcs  = set()
    chunks     = []

    def _add(rows) -> None:
        for row in rows:
            key = f"{row['source_type']}:{row['source']}"
            if key not in seen_srcs:
                seen_srcs.add(key)
                chunks.append(row)

    rag_types = "('docs','scene_config','env_keys')"

    if fts_q:
        # 1. AND query (all terms must appear)
        try:
            _add(conn.execute(
                f"""SELECT m.source_type, m.source, m.content
                    FROM memories_fts f JOIN memories m ON m.id = f.rowid
                    WHERE memories_fts MATCH ?
                    AND m.source_type IN {rag_types}
                    ORDER BY rank LIMIT 5""",
                (fts_q,),
            ).fetchall())
        except Exception as exc:
            log.debug("FTS AND failed: %s", exc)

        # 2. OR query fallback (any term matches) — handles synonym misses like "retire" vs "removed"
        if len(chunks) < 2:
            or_q = " OR ".join(w for w in fts_q.split() if len(w) > 3)
            if or_q and or_q != fts_q:
                try:
                    _add(conn.execute(
                        f"""SELECT m.source_type, m.source, m.content
                            FROM memories_fts f JOIN memories m ON m.id = f.rowid
                            WHERE memories_fts MATCH ?
                            AND m.source_type IN {rag_types}
                            ORDER BY rank LIMIT 5""",
                        (or_q,),
                    ).fetchall())
                except Exception as exc:
                    log.debug("FTS OR failed: %s", exc)

        # 3. LIKE fallback on the most specific word
        if not chunks:
            word = max(fts_q.split(), key=len, default="")
            if word:
                try:
                    _add(conn.execute(
                        f"""SELECT source_type, source, content FROM memories
                            WHERE source_type IN {rag_types}
                            AND content LIKE ?
                            ORDER BY id DESC LIMIT 5""",
                        (f"%{word}%",),
                    ).fetchall())
                except Exception:
                    pass

    # Always fetch recent diary entries — their content isn't keyword-indexed meaningfully
    try:
        _add(conn.execute(
            """SELECT source_type, source, content FROM memories
               WHERE source_type = 'diary'
               ORDER BY timestamp DESC LIMIT 3"""
        ).fetchall())
    except Exception:
        # source_type not yet added (rag_index.py not yet run); fall back to role
        try:
            _add(conn.execute(
                """SELECT 'diary' AS source_type, source, content FROM memories
                   WHERE role = 'diary'
                   ORDER BY timestamp DESC LIMIT 3"""
            ).fetchall())
        except Exception:
            pass

    if not chunks:
        return (
            "I couldn't find relevant information in my knowledge base. "
            "Try running rag_index.py to populate the RAG index first."
        )

    context_parts = []
    for row in chunks:
        label = f"{row['source_type']} / {row['source']}"
        context_parts.append(f"[{label}]\n{row['content'][:600]}")

    context = "\n\n---\n\n".join(context_parts)

    prompt = (
        "Answer this question about the Neo smart home system using ONLY the provided context. "
        "Cite which source the answer comes from (e.g. 'According to CLAUDE.md...'). "
        "Be concise and direct.\n\n"
        f"Question: {question}\n\n"
        f"Context:\n{context}"
    )

    try:
        resp = _get_claude().messages.create(
            model=MODEL,
            max_tokens=450,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as exc:
        log.error("_neo_rag_answer Claude call failed: %s", exc)
        return f"RAG answer failed: {exc}"


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

    services = ["hub", "wiz-lamp", "voice", "tgvoice"]
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


def _memory_context(text: str) -> tuple[list, list]:
    """Return (recent_history, relevant_memories) for enriching Claude calls."""
    try:
        history = memory.get_recent(15)
        ctx     = memory.search(text, n=3)
        return history, ctx
    except Exception:
        return [], []


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
    try:
        memory.store_conversation("user", f"[voice] {transcript}")
    except Exception:
        pass
    history, ctx = _memory_context(transcript)
    actions  = resolve_intent(transcript, history=history, mem_context=ctx)

    if len(actions) == 1 and actions[0].get("action") == "ask_neo":
        log.info("ASK_NEO intent detected")
        answer = _neo_rag_answer(transcript)
        reply  = f'🗣 "{transcript}"\n\n🧠 {answer}'
        try:
            memory.store_conversation("assistant", answer)
        except Exception:
            pass
        await msg.reply_text(reply)
        return

    if all(not a.get("action") for a in actions):
        rag_lines, rag_reply = _rag_fallback(transcript)
        results = rag_lines or dispatch(actions)
        extra   = f"{rag_reply}\n" if rag_reply else ""
    else:
        results = dispatch(actions)
        extra   = ""
    reply = f'🗣 "{transcript}"\n{extra}' + "\n".join(results)
    try:
        memory.store_conversation("assistant", "\n".join(results))
    except Exception:
        pass
    await msg.reply_text(reply)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not _is_allowed(msg):
        return

    transcript = msg.text.strip()
    if not transcript:
        return

    log.info('Text: "%s"', transcript)
    try:
        memory.store_conversation("user", transcript)
    except Exception:
        pass
    history, ctx = _memory_context(transcript)
    actions  = resolve_intent(transcript, history=history, mem_context=ctx)

    if len(actions) == 1 and actions[0].get("action") == "ask_neo":
        log.info("ASK_NEO intent detected")
        answer = _neo_rag_answer(transcript)
        reply  = f'💬 "{transcript}"\n\n🧠 {answer}'
        try:
            memory.store_conversation("assistant", answer)
        except Exception:
            pass
        await msg.reply_text(reply)
        return

    if all(not a.get("action") for a in actions):
        rag_lines, rag_reply = _rag_fallback(transcript)
        results = rag_lines or dispatch(actions)
        extra   = f"{rag_reply}\n" if rag_reply else ""
    else:
        results = dispatch(actions)
        extra   = ""
    reply = f'💬 "{transcript}"\n{extra}' + "\n".join(results)
    try:
        memory.store_conversation("assistant", "\n".join(results))
    except Exception:
        pass
    await msg.reply_text(reply)


# ---------------------------------------------------------------------------
# Memory commands
# ---------------------------------------------------------------------------

async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search long-term memory: /memory <query>"""
    msg = update.message
    if not msg or not _is_allowed(msg):
        return
    query = " ".join(context.args or []).strip()
    if not query:
        await msg.reply_text("Usage: /memory <query>")
        return
    try:
        results = memory.search(query, n=5)
        if not results:
            await msg.reply_text("No memories found.")
            return
        lines = [f"🔍 *Memory search: {query}*"]
        for i, r in enumerate(results, 1):
            ts = r.get("timestamp", "")[:16]
            lines.append(f"{i}. [{ts}] {r['content'][:200]}")
        await msg.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        await msg.reply_text(f"Memory search failed: {exc}")


async def cmd_scene_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show last 10 scene activations: /scene_history"""
    msg = update.message
    if not msg or not _is_allowed(msg):
        return
    try:
        log_entries = memory.get_scene_history(n=10)
        if not log_entries:
            await msg.reply_text("No scene history yet.")
            return
        lines = ["*Recent scene activations:*"]
        for e in log_entries:
            ts = e.get("timestamp", "")[:16]
            lines.append(f"• `{e['scene']}` via {e['triggered_by']} at {ts}")
        await msg.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        await msg.reply_text(f"Scene history failed: {exc}")


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear conversation history (not long-term memories): /forget"""
    msg = update.message
    if not msg or not _is_allowed(msg):
        return
    try:
        c = memory._conn()
        c.execute("DELETE FROM conversation")
        c.commit()
        await msg.reply_text("✅ Conversation history cleared. Long-term memories kept.")
    except Exception as exc:
        await msg.reply_text(f"Forget failed: {exc}")


async def cmd_remember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store something as a long-term memory: /remember <text>"""
    msg = update.message
    if not msg or not _is_allowed(msg):
        return
    text = " ".join(context.args or []).strip()
    if not text:
        await msg.reply_text("Usage: /remember <text to remember>")
        return
    try:
        mid = memory.store_memory(text, role="user", source="telegram_manual")
        await msg.reply_text(f"✅ Stored as memory #{mid}.")
    except Exception as exc:
        await msg.reply_text(f"Remember failed: {exc}")


# ---------------------------------------------------------------------------
# /diary — retrieve Neo's diary entries
# ---------------------------------------------------------------------------

async def cmd_diary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/diary | /diary <YYYY-MM-DD> | /diary all"""
    msg = update.message
    if not msg or not _is_allowed(msg):
        return

    arg = " ".join(context.args or []).strip().lower()

    try:
        c = memory._conn()

        if arg == "all":
            rows = c.execute(
                "SELECT source, timestamp FROM memories "
                "WHERE role = 'diary' ORDER BY timestamp DESC"
            ).fetchall()
            if not rows:
                await msg.reply_text("📓 No diary entries yet. Neo has been living in the moment.")
                return
            lines = ["📓 *Diary entries available:*"]
            for row in rows:
                date_str = row["source"].replace("neo_diary_", "")
                try:
                    from datetime import datetime as _dt
                    display = _dt.strptime(date_str, "%Y-%m-%d").strftime("%-d %b %Y")
                except Exception:
                    display = date_str
                lines.append(f"• {display}")
            await msg.reply_text("\n".join(lines), parse_mode="Markdown")
            return

        if arg and arg != "today":
            # Specific date: accept YYYY-MM-DD
            source_key = f"neo_diary_{arg}"
            row = c.execute(
                "SELECT content, source, timestamp FROM memories "
                "WHERE role = 'diary' AND source = ? LIMIT 1",
                (source_key,),
            ).fetchone()
            if not row:
                await msg.reply_text(
                    f"Neo has no memory of {arg}. "
                    "Perhaps nothing happened. Perhaps Neo has chosen to forget."
                )
                return
            try:
                from datetime import datetime as _dt
                display = _dt.strptime(arg, "%Y-%m-%d").strftime("%-d %B %Y")
            except Exception:
                display = arg
            await msg.reply_text(
                f"📓 *Neo's Diary — {display}*\n\n{row['content']}",
                parse_mode="Markdown",
            )
            return

        # Latest entry (default)
        row = c.execute(
            "SELECT content, source, timestamp FROM memories "
            "WHERE role = 'diary' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        if not row:
            await msg.reply_text("📓 No diary entries yet. Neo is still gathering its thoughts.")
            return
        date_str = row["source"].replace("neo_diary_", "")
        try:
            from datetime import datetime as _dt
            display = _dt.strptime(date_str, "%Y-%m-%d").strftime("%-d %B %Y")
        except Exception:
            display = date_str
        await msg.reply_text(
            f"📓 *Neo's Diary — {display}*\n\n{row['content']}",
            parse_mode="Markdown",
        )

    except Exception as exc:
        log.error("cmd_diary failed: %s", exc)
        await msg.reply_text("Neo is unable to retrieve the diary right now.")


# ---------------------------------------------------------------------------
# /repair — self-repair loop for scheduled tasks (Upgrade 5)
# ---------------------------------------------------------------------------

_TASKS_DIR = Path(__file__).parent / "tasks"
# Pending repairs keyed by chat_id: {task_name, fixed_code, task_path}
_pending_repairs: dict[int, dict] = {}


async def cmd_repair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/repair <task_name> — run task, fix errors with Claude, confirm before writing."""
    msg = update.message
    if not msg or not _is_allowed(msg):
        return
    task_name = " ".join(context.args or []).strip().rstrip(".py")
    if not task_name:
        await msg.reply_text("Usage: /repair <task_name>\nExample: /repair morning_brief")
        return

    task_path = _TASKS_DIR / f"{task_name}.py"
    if not task_path.exists():
        await msg.reply_text(f"Task not found: {task_path}")
        return

    await msg.reply_text(f"🔧 Running `{task_name}`…", parse_mode="Markdown")

    result = subprocess.run(
        [sys.executable, str(task_path)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        await msg.reply_text(f"✅ `{task_name}` ran successfully.\n{result.stdout[:500]}", parse_mode="Markdown")
        return

    error_output = (result.stdout + result.stderr)[:1500]
    file_contents = task_path.read_text()
    await msg.reply_text(f"❌ Error:\n```\n{error_output[:800]}\n```\nAsking Claude for a fix…", parse_mode="Markdown")

    prompt = (
        f"This Python task failed with the following error. Read the file, "
        f"identify the bug, and return ONLY the corrected Python code with "
        f"no explanation:\n\nError:\n{error_output}\n\nFile contents:\n{file_contents}"
    )
    try:
        claude_resp = _get_claude().messages.create(
            model=MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        fixed_code = claude_resp.content[0].text.strip()
        if fixed_code.startswith("```"):
            fixed_code = fixed_code.split("```")[1]
            if fixed_code.startswith("python"):
                fixed_code = fixed_code[6:]
            fixed_code = fixed_code.strip()
    except Exception as exc:
        await msg.reply_text(f"Claude API error: {exc}")
        return

    _pending_repairs[msg.chat_id] = {
        "task_name": task_name,
        "task_path": task_path,
        "fixed_code": fixed_code,
    }
    preview = fixed_code[:1200]
    await msg.reply_text(
        f"📝 Proposed fix:\n```python\n{preview}\n```\n\n"
        f"Reply /confirm to apply and re-run, or /cancel to discard.",
        parse_mode="Markdown",
    )


async def cmd_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/confirm — apply pending /repair fix and re-run the task."""
    msg = update.message
    if not msg or not _is_allowed(msg):
        return
    pending = _pending_repairs.pop(msg.chat_id, None)
    if not pending:
        await msg.reply_text("No pending repair. Use /repair <task_name> first.")
        return

    task_path  = pending["task_path"]
    fixed_code = pending["fixed_code"]

    bak = backup.backup_file(task_path)
    task_path.write_text(fixed_code)
    await msg.reply_text(f"✅ Fix applied (backup: `{bak.name if bak else 'none'}`). Re-running…", parse_mode="Markdown")

    result = subprocess.run(
        [sys.executable, str(task_path)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        await msg.reply_text(f"✅ `{pending['task_name']}` now runs successfully!", parse_mode="Markdown")
    else:
        out = (result.stdout + result.stderr)[:800]
        await msg.reply_text(f"❌ Still failing:\n```\n{out}\n```", parse_mode="Markdown")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cancel — discard pending /repair fix."""
    msg = update.message
    if not msg or not _is_allowed(msg):
        return
    if _pending_repairs.pop(msg.chat_id, None):
        await msg.reply_text("🚫 Repair discarded. No files changed.")
    else:
        await msg.reply_text("Nothing pending.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set in notifier.env")
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        raise SystemExit("ANTHROPIC_API_KEY not set in voice.env")

    memory.init()
    log.info("Memory store initialised (DB: %s)", memory._DB_PATH)

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
    app.add_handler(CommandHandler("status",        handle_status))
    app.add_handler(CommandHandler("memory",        cmd_memory))
    app.add_handler(CommandHandler("scene_history", cmd_scene_history))
    app.add_handler(CommandHandler("forget",        cmd_forget))
    app.add_handler(CommandHandler("remember",      cmd_remember))
    app.add_handler(CommandHandler("diary",         cmd_diary))
    app.add_handler(CommandHandler("repair",        cmd_repair))
    app.add_handler(CommandHandler("confirm",       cmd_confirm))
    app.add_handler(CommandHandler("cancel",        cmd_cancel))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    if app.job_queue and ALLOWED_CHAT:
        app.job_queue.run_repeating(send_tip, interval=7200, first=60)
        log.info("Tip scheduler active — every 2h, first in 60s")

    log.info("Bot started — polling (chat_id=%s)", ALLOWED_CHAT or "any")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
