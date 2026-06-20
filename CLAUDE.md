# Neo — Smart Home Hub (CLAUDE.md)

> Drop this file gives any Claude Code session instant full context about the Neo smart home system.
> Last updated: 2026-06-20 (audit system live; institutionalized skills/ with diagnostic utilities and lessons learned)

---

## Before You Start

Check `skills/` for relevant guidance before beginning any task.
`skills/lessons-learned/SKILL.md` and `skills/before-shipping/SKILL.md` apply to almost everything.
`skills/diagnose/check_services.sh` is the first step for any debugging or sitrep request.

---

## What This Is

A Raspberry Pi (hostname **Neo**, IP **192.168.1.8**) runs a multi-service smart home automation hub 24/7. It controls a Samsung TV and a WiZ smart bulb through a central Flask HTTP API, with two voice intelligence layers: a local VAD pipeline (`voice.py` via Termux microphone) and a Telegram bot (`tgvoice.py`) that accepts both text and voice messages via Whisper + Claude API. A JBL Flip 4 Bluetooth speaker is connected for TTS audio output via Piper (neural, offline). An independent audit system periodically samples Claude's behaviour across 30 questions to detect model drift.

---

## Hardware

| Device | IP / MAC | Role |
|---|---|---|
| Raspberry Pi (Neo) | 192.168.1.8 | All services; runs 24/7 |
| Samsung TV (Tizen) | 192.168.1.2 | WebSocket :8002 (keys) + REST :8001 (apps/status) |
| WiZ smart bulb | 192.168.1.9 | UDP via pywizlight |
| Termux mic (Android) | — | Voice input via `termux-microphone-record` → `/api/voice` endpoint |
| JBL Flip 4 | 6C:47:60:AA:21:DE | Bluetooth speaker — TTS audio output via Piper |

---

## Services

| Service | File | Port | Status |
|---|---|---|---|
| `hub.service` | `smarthome/hub.py` | 5001 | **active** |
| `wiz-lamp.service` | `smarthome/wiz-lamp/app.py` | 5000 | **active** |
| `voice.service` | `smarthome/voice.py` | — | **inactive** (Termux mic approach preferred) |
| `tgvoice.service` | `smarthome/tgvoice.py` | — | **active** |
| `bt_jbl.service` | `/etc/systemd/system/bt_jbl.service` | — | **active** (auto-connects JBL on boot) |

**Golden rule:** always call the hub (port 5001), never the lamp service directly. The hub proxies `/lamp/*` to port 5000, so scenes stay coordinated.

---

## Architecture

```
┌────────────────────────────────────────────┐
│               Raspberry Pi (Neo)            │
│                                            │
│  voice.py ──────────────────────────────── │  ← Termux mic → /api/voice → Claude API → hub
│  tgvoice.py ────────────────────────────── │  ← Telegram text/voice → Whisper → Claude API → hub
│  neo_mic.py (Termux node) ──────────────── │  ← audio stream handler for termux-microphone-record
│  bt_presence.py ────────────────────────── │  ← BT scanner for phone arrival/departure
│  audit.py ──────────────────────────────── │  ← Claude model drift detection (30-question battery)
│                                            │
│  ┌──────────────────────────────────────┐  │
│  │   hub.py  :5001  (central API)       │  │
│  │   scenes / TV / Spotify / NFC / lamp │  │
│  └──────────────┬───────────────────────┘  │
│                 │ proxy /lamp/*             │
│  ┌──────────────▼───────────────────────┐  │
│  │   wiz-lamp/app.py  :5000             │  │
│  │   effects / transitions / static     │  │
│  └──────────────────────────────────────┘  │
│                                            │
└───────────────┬────────────────────────────┘
                │
     ┌──────────┴──────────┐
     ▼                     ▼
Samsung TV (192.168.1.2)  WiZ Bulb (192.168.1.9)
WebSocket :8002 + REST :8001    UDP (pywizlight)
```

---

## File Layout

```
smarthome/
├── hub.py                Central Flask API (port 5001)
├── tv.py                 Samsung TV driver (samsungtvws + WoL)
├── spotify.py            Spotify Web API wrapper (OAuth, playback)
├── beat_sync.py          BPM-driven lamp pulse synced to music
├── scenes.json           Scene definitions — edit without touching Python
├── tags.json             NFC UID → scene mappings (runtime-updated)
├── presence.json         Current presence state (home / away)
├── spotify_tokens.json   Spotify OAuth tokens (scopes: playback, library)
├── spotify.env           SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET
├── notifier.env          TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
├── voice.py              Voice pipeline: Termux mic → /api/voice → Whisper → Claude → hub
├── tgvoice.py            Telegram bot: text/voice → Whisper → Claude → hub
├── voice.env             ANTHROPIC_API_KEY + voice tuning (copy from .example)
├── voice.env.example     Template for voice.env
├── voice.service         systemd unit for voice.py
├── tgvoice.service       systemd unit for tgvoice.py
├── hub.service           systemd unit for hub.py
├── install.sh            Installs hub.service + venv
├── tts.py                Piper TTS engine — speak() / speak_async() → JBL via PipeWire
├── scene_rag.py          RAG pipeline: TF-IDF retrieval + Claude generation for semantic scene matching
├── memory.py             Event log + diary + contextual memory for enhanced decision-making
├── scheduler.py          Background job scheduler for recurring tasks (tips, reminders, etc.)
├── rag_index.py          RAG document indexing and FTS5 corpus builder for self-knowledge
├── hub.env               JBL_MAC, JBL_NAME, TTS_ENABLED, TTS_MAX_WORDS, NEO_API_KEY, BT_PHONE_MAC
├── bt_pair.py            Headless Bluetooth pairing helper (manual, not integrated)
├── bt_presence.py        Bluetooth presence detection scanner (enabled — detects phone arrival/departure)
├── auth.py               API authentication utilities
├── utils.py              Shared helpers (parsing, state management)
├── backup.py             State/config backup utilities
├── demo.py               Demo/test harness
├── update_claude_md.py   Auto-sync CLAUDE.md with current architecture
├── audit.py              Claude model drift detection — 30-question battery sampled every 72h
├── audit_battery.json    Immutable 30-question baseline for audit system
├── requirements.txt      Hub Python deps
├── venv/                 Python virtual environment (shared by all services)
│
├── skills/               Institutional knowledge, checklists, and diagnostic utilities
│   ├── architecture-philosophy/SKILL.md   Design principles and rationale
│   ├── before-shipping/SKILL.md           Pre-deployment checklist and review criteria
│   ├── diagnose/SKILL.md                  Troubleshooting methodology
│   ├── diagnose/check_services.sh         One-command service health and diagnostics
│   ├── interaction-style/SKILL.md         Communication style for Neo (voice/text)
│   └── lessons-learned/SKILL.md           Historical gotchas, failure modes, anti-patterns
│
├── termux/
│   ├── neo_mic.py        Termux microphone stream handler + VAD
│   ├── install.sh        Setup script for Termux dependencies
│
├── piper/                Piper TTS binary + jenny-dioco voice (local only, gitignored)
│   ├── piper/piper       Piper binary (aarch64)
│   └── voices/en_GB-jenny_dioco-medium.onnx
│
├── wakeword/             Wake-word detection (future integration)
│   └── install.sh        Setup script for wake-word engine
│
├── tasks/                Background task scripts
│   ├── morning_brief.py  Morning briefing generation
│   ├── rag_reindex.py    Periodic RAG corpus rebuild (weekly Sunday 4am)
│   └── anthropic_audit.py Scheduled audit runner (every 72h at 03:00)
│
├── neo-labs/             Experimental features (dashboard, advanced search, etc.)
│   └── (various prototypes)
│
└── wiz-lamp/
    ├── app.py            WiZ lamp Flask API (port 5000)
    ├── config.env        LAMP_IP=192.168.1.9
    ├── wiz-lamp.service  systemd unit
    └── install.sh        Installs wiz-lamp.service
```

---

## Hub API Reference (port 5001)

### Scenes — `GET /scene/<name>`

Scenes run lamp + TV in parallel threads. Defined in `scenes.json`.

| Scene | Lamp | TV | Notes |
|---|---|---|---|
| `movie` | warm white 30% (2700K) | on → Prime, vol 15 | |
| `netflix` | warm white 30% | on → Netflix, vol 15, auto-Enter profile | post_launch KEY_ENTER |
| `youtube` | relax (2400K 40%) | on → YouTube, vol 12 | |
| `youtube-music` | morning (5000K 70%) | on → YouTube playlist, ramp 20→60 over 2 min | deep-link playlist |
| `focus` | cool white 100% (6500K) | off | |
| `relax` | soft amber 40% (2400K) | on, vol 10 | |
| `goodnight` | goodnight fade (5.5 min) | off | |
| `morning` | wake transition (90s) | on → YouTube, vol 8 | |
| `party` | party colour cycle | on → Spotify, vol 20 | |
| `off` | off | off | |
| `sunset` | sunset transition (5.5 min) | off | |
| `dinner` | candlelight 50% (2500K) | off | |
| `gaming` | blue-white 60% | on, vol 18 | |
| `romance` | deep red 15% | off | |
| `reading` | neutral white 80% (4000K) | off | |
| `music` | pulse effect | on → Spotify, ramp 20→60 over 2 min | |
| `leave` | off | off | sets presence → away |
| `thunderstruck` | pure blue 80% | on → Spotify vol 40 | plays AC/DC track, 12s delay |

`GET /scenes` — list all available scene names.

### TV — `GET /tv/<endpoint>`

```
Power:     /tv/on    /tv/off    /tv/status
Audio:     /tv/mute  /tv/volume/<n>   (n positive=up, negative=down; relative steps)
           (absolute volume is not a direct endpoint — use scene volume_abs or tv_set_abs_volume internally)
Source:    /tv/source/hdmi1   hdmi2   hdmi3   hdmi4   tv   av
Apps:      /tv/app/netflix    youtube   prime   spotify   appletv
           /tv/apps   (list known apps + IDs)
Playback:  /tv/play  /tv/pause  /tv/stop  /tv/ff  /tv/rewind  /tv/next  /tv/prev
Nav:       /tv/home  /tv/back  /tv/up  /tv/down  /tv/left  /tv/right  /tv/enter
Raw key:   /tv/key/<KEY_CODE>   (any Samsung Tizen key code)
```

**tv_on() is state-aware:** fully off → Wake-on-LAN; standby → KEY_POWER; already on → no-op.  
**Absolute volume** (`tv_set_abs_volume`): hammers KEY_VOLUMEDOWN ×60 to zero, then counts up. Takes several seconds but reliable.  
**Volume ramp** runs in a background thread with a watchdog (polls every 4s, stops if TV goes standby).

### Lamp — `GET /lamp/<endpoint>`

Hub proxies to wiz-lamp on port 5000. See the Lamp API section below for the full list.

### Spotify — `GET /spotify/<endpoint>`

```
/spotify/status       Current track, device, volume
/spotify/play         Resume (or ?uri=spotify:track:... to play specific URI)
/spotify/pause
/spotify/next  /prev
/spotify/volume/<0-100>
/spotify/shuffle/<on|off>
/spotify/repeat/<off|track|context>
/spotify/search/<query>
/spotify/devices
/spotify/auth          → redirects to OAuth (first-time setup)
/spotify/exchange?code=  Manual code exchange (headless)
```

Tokens stored in `spotify_tokens.json`. Scopes: user-read-playback-state, user-modify-playback-state, user-read-currently-playing, playlist-read-private, user-library-read.

### Beat Sync — `GET /spotify/beat-sync/<endpoint>`

```
/spotify/beat-sync/on        Start pulsing lamp to BPM (default 120)
/spotify/beat-sync/off
/spotify/beat-sync/bpm/<n>   Set BPM and restart
/spotify/beat-sync/status    {"running", "bpm"}
```

BPM is user-supplied — Spotify's audio analysis API is restricted to pre-Nov-2024 apps, so beat timestamps aren't available.

### NFC — `POST /nfc/scan`, etc.

```
POST /nfc/scan      {"uid": "<tag UID>"}   → execute mapped scene
POST /nfc/register  {"uid": "...", "scene": "..."}
GET  /nfc/tags      list registered tags + available scenes
GET  /tag/<uid>               legacy GET trigger
GET  /tag/<uid>/<scene>       legacy register
GET  /tags                    legacy list
```

Registered tags (in `tags.json`):
- `AABBCCDD` → movie (test/placeholder)
- `53299FDF730001` → leave (real tag, on door/keychain)

UIDs are normalised (uppercase, colons stripped) — any reader format works.

### Presence — `GET/POST /presence`

```
GET  /presence              → {"state": "home|away", "updated": "..."}
POST /presence  {"state": "home|away"}
```

Currently: `away` (last updated 2026-06-03). Updated automatically by the `leave` scene or BT presence detection.

### TTS — `GET /tts/<endpoint>`

```
/tts/on      Enable TTS (writes TTS_ENABLED=true to hub.env, takes effect immediately)
/tts/off     Disable TTS
/tts/status  {"tts_enabled", "jbl_connected", "jbl_mac", "active_sink"}
```

TTS is **off by default**. Toggle without restarting hub — hub.env is read live on every call.

### Announce — `POST /api/announce`

```json
{"text": "your message here", "device": "jbl"}
```

Speaks text via Piper (jenny-dioco) through JBL Flip 4. `device` defaults to `"jbl"`. Max 500 chars. Non-blocking — returns immediately while audio plays in background. Silently skipped if JBL is disconnected or TTS is disabled.

Scene guard: speech suppressed automatically during `movie`, `netflix`, `goodnight`, `sleep`, `dnd` scenes.

### Voice — `POST /api/voice`

```json
{"audio_base64": "<raw PCM audio>", "sample_rate": 16000}
```

Accepts raw 16-bit mono PCM audio (from Termux microphone stream), transcribes via Whisper, sends to Claude for intent resolution, and dispatches hub actions. Returns:

```json
{"transcript": "...", "action": "/scene/...", "reason": "..."}
```

Used by `termux/neo_mic.py` to stream audio from Android device microphone to Neo via HTTP.

### Event Hooks — `GET /event/<name>`

Contextual triggers that check time/state before acting.

| Endpoint | Condition | Action |
|---|---|---|
| `/event/swiggy` | 23:00–00:59 | `/lamp/alert` + TTS "Beta, you don't need maggi at midnight." |
| `/event/swiggy` | any other time | 204 No Content (silent no-op) |

Intended to be called from an iPhone Shortcut triggered on Swiggy app open.

### Shortcuts — `GET /shortcuts`

Returns all scene/lamp/tv/spotify URLs pre-formatted for iOS Shortcuts setup.

---

## Lamp API Reference (port 5000, or via /lamp/* on port 5001)

### Static Scenes

| Endpoint | Colour Temp | Brightness | Notes |
|---|---|---|---|
| `/on` | default | 100% | |
| `/off` | — | — | cancels any effect |
| `/focus` | 6500K cool white | 100% | |
| `/morning` | 5000K daylight | 70% | |
| `/reading` | 4000K neutral | 80% | |
| `/relax` | 2400K amber | 40% | |
| `/dinner` | 2500K candlelight | 50% | |
| `/movie` | 2700K warm | 30% | |
| `/sleep` | 2200K very warm | 10% | |
| `/romance` | RGB (220, 30, 10) | 15% | deep red |
| `/gaming` | RGB (60, 120, 255) | 60% | blue-white |
| `/blue` | RGB (0, 0, 255) | 80% | pure blue |
| `/brightness/<0-100>` | current | set % | |

### Looping Effects (run until `/off` or next command)

| Endpoint | Description |
|---|---|
| `/blink` | Fast on/off flash |
| `/pulse` | Slow breathing dim↔bright |
| `/party` | Random colour cycling |
| `/alert` | Red SOS flash (3×short, 3×long, 3×short) |
| `/strobe` | Fast white strobe |
| `/candle` | Warm flicker |
| `/campfire` | Wilder/brighter flicker |
| `/aurora` | Slow northern-lights colour drift (~48s cycle) |
| `/disco` | Rainbow hue sweep + sine brightness + random white flash |

### Transitions (run once, settle at final state or off)

| Endpoint | Description | Duration |
|---|---|---|
| `/wake` | Dim warm (2200K) → full daylight (6500K) | ~90s |
| `/bedtime` | Medium → warm dim sleep level | ~2 min |
| `/fade` | Current → off | ~30s |
| `/sunrise` | Deep red → orange → warm white | ~5 min |
| `/sunset` | Golden → deep red → off | ~5.5 min |
| `/goodnight` | Long peaceful fade to off | ~5.5 min |

`GET /status` — returns `{on, brightness_pct, colortemp_k, rgb, effect}`.

**Effect engine:** each effect runs in a background thread. Any new command cancels the current effect first. A watchdog polls the bulb every 3s; if another device turns the lamp off (and the Pi didn't send that off within a 2s grace period), the watchdog stops the effect and sends an explicit turn_off — preventing in-flight iterations from re-enabling the lamp.

---

## TTS / Audio Output (`tts.py`)

**Engine:** Piper TTS (offline neural) → `aplay` → PipeWire → JBL Flip 4 over Bluetooth.  
**Voice:** `en_GB-jenny_dioco-medium` — warm British female, `--length-scale 1.1` (0.9× speed).  
**Fallback:** espeak-ng (robotic, only if Piper binary missing).

```
POST /api/announce → tts.speak_async(text) → Piper → aplay → PipeWire → JBL
```

Key behaviours:
- `TTS_ENABLED` and `TTS_MAX_WORDS` read live from `hub.env` — toggle takes effect instantly
- JBL disconnected → silent skip, never crashes
- Scene guard suppresses speech during: `movie`, `netflix`, `goodnight`, `sleep`, `dnd`
- TTS is for **announcements and morning brief only** — scene activations do NOT speak
- `set_current_scene()` is called on every scene change (for the guard), but no audio

Piper binary and voice model are in `piper/` (gitignored, device-local). Install path:
- Binary: `smarthome/piper/piper/piper`
- Voice: `smarthome/piper/voices/en_GB-jenny_dioco-medium.onnx`
- Libs: `LD_LIBRARY_PATH=smarthome/piper/piper`

---

## Voice Pipeline (`voice.py` + Termux mic)

**Architecture:**
```
Android device (Termux) → termux-microphone-record → neo_mic.py
                                                    → POST /api/voice
                                                    ↓
                                        Whisper base (local, serialised)
                                        ↓
                                        Claude API (claude-sonnet-4-20250514)
                                        ↓
                                        JSON parse → GET hub endpoints
```

**Local Pipeline (older, currently inactive):**
```
3.5mm mic → PyAudio (16kHz int16 mono)
          → WebRTC VAD (30ms frames, aggressiveness 0–3)
          → ring-buffer pre-speech padding (300ms)
          → silence detection (900ms ends utterance)
          → Whisper base
          → Claude API
          → JSON parse → GET hub endpoints
```

**Current approach:** Termux microphone stream via `neo_mic.py` is preferred. The local `voice.py` pipeline is inactive due to USB mic device issues.

Claude receives a system prompt built at startup from `scenes.json` + all hub routes. It responds with strict JSON only — no hardcoded phrase mapping anywhere.

Response format:
```json
{"action": "/scene/movie", "reason": "user wants to watch"}
// or multi-step:
[{"action": "/spotify/pause", "reason": "..."}, {"action": "/scene/focus", "reason": "..."}]
// or no-op:
{"action": null, "reason": "unclear"}
```

Config in `voice.env` (copy from `voice.env.example`):
- `ANTHROPIC_API_KEY` — required
- `VAD_MODE` — 0–3 (default 2; raise to 3 in noisy rooms)
- `WHISPER_MODEL` — base (default); tiny is faster, small is more accurate
- `SILENCE_TIMEOUT` — default 0.9s
- `CLAUDE_MODEL` — default `claude-sonnet-4-20250514`

---

## Telegram Voice Bot (`tgvoice.py`)

Telegram bot running as `tgvoice.service`. Accepts messages from the allowlisted chat ID only (`TELEGRAM_CHAT_ID`).

**Message handling:**
- **Text messages** → Claude intent → hub endpoint(s) → reply with result
- **Voice messages** (OGG) → ffmpeg → WAV → Whisper → Claude intent → hub → reply with transcript + result
- **RAG fallback** — if Claude returns `action: null`, `scene_rag.run()` is called: TF-IDF retrieves top-3 scenes, Claude picks the best, scene is dispatched with a natural-language reply
- **ASK_NEO intent** — Claude detects self-knowledge questions and routes to FTS5 index over CLAUDE.md, scenes.json, docstrings, and recent diary entries; replies with sourced answers
- **`/status` command** → live service health + device states (lamp, TV, Spotify, presence)

**`/status` output:**
- Per-service active/inactive for: hub, wiz-lamp, voice, tgvoice
- Lamp: on/off · colour temp · brightness · active effect (if any)
- TV: power state
- Spotify: now-playing track + artist, or "not playing"
- Presence: home / away

**Scheduled tips:**
- Claude generates one Neo capability tip every 2 hours and sends it to `ALLOWED_CHAT`
- Tips highlight scenes, lamp effects, and lesser-known features
- First tip fires 60s after bot startup; interval is 7200s
- Requires `ALLOWED_CHAT` to be set and APScheduler installed (`python-telegram-bot[job-queue]`)

Uses the same system prompt as `voice.py` (auto-built at startup from `scenes.json` + all hub routes). Claude returns strict JSON actions; no hardcoded phrase mapping.

Config: reads `voice.env` (ANTHROPIC_API_KEY, WHISPER_MODEL, CLAUDE_MODEL) and `notifier.env` (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).

---

## Self-Knowledge RAG (`rag_index.py` + ASK_NEO intent)

Neo can answer questions about its own architecture, configuration, and history using a RAG pipeline backed by SQLite FTS5.

**Indexed sources** (stored in `memory.db` with `source_type` column):
- `docs` — CLAUDE.md chunked by `##` headings (~400 words/chunk) + Python file docstrings
- `scene_config` — `scenes.json` as one summary chunk + per-scene detail chunks
- `env_keys` — KEY names only from all `.env` files (values never stored)
- `diary` — Neo's nightly diary entries (written by `tasks/morning_brief.py`)

**Indexing:** Run `python rag_index.py` once to populate; adds `source_type` column to `memories` table. Weekly re-index runs automatically via `tasks/rag_reindex.py` (Sunday 4am).

**ASK_NEO intent in `tgvoice.py`:** When Claude detects a self-knowledge question it returns `{"action": "ask_neo", "reason": "..."}`. The bot then:
1. Sanitises the question into FTS5 terms (strips stop words and punctuation)
2. Runs AND FTS5 query across docs/scene_config/env_keys; falls back to OR query if `< 2` results
3. Always appends the 3 most recent diary entries (diary content isn't keyword-searchable)
4. Passes all context chunks to Claude: "answer using ONLY the provided context, cite the source"
5. Replies to Telegram with the sourced answer

Example triggers: "why did we retire the wakeword service", "what's in voice.env", "what did Neo write in its diary recently"

---

## Semantic Intelligence (`scene_rag.py`)

Two-step RAG pipeline for vague user intents:

1. **TF-IDF retrieval** — no external API calls; retrieves top-3 scenes from `SCENE_KB` (18 rich scene descriptions with synonym tags)
2. **Claude generation** — one API call to pick the best match and compose a natural-language explanation

Wired into `tgvoice.py` as a fallback when Claude intent returns `action: null`. Handles vague phrases like "something cozy" or "I want to dance" — retrieval finds relevant scenes, Claude picks one and explains why.

---

## RAG Indexing (`rag_index.py`)

TF-IDF corpus builder for scene retrieval. Indexed documents stored in memory; no persistent DB required. Used by `scene_rag.py` during runtime initialization and by `tasks/rag_reindex.py` for periodic rebuilds.

---

## Event Log & Memory (`memory.py`)

Simple event log + diary system for contextual decision-making. Stores:
- Scene activations + timestamps
- Voice/text command intents
- Tap-recorded diary entries (future: morning-briefing context)

Used by future enhancements to add time-of-day bias and user preference learning.

---

## Scheduler (`scheduler.py`)

Background job scheduler for recurring tasks:
- Scheduled tips in Telegram bot
- Future: morning briefing generation, reminder timing, circadian light control

Uses APScheduler (job queue required for `python-telegram-bot`).

---

## Bluetooth Presence Detection (`bt_presence.py`)

Bluetooth scanner that detects phone arrival/departure and triggers corresponding scenes:
- Phone MAC configured in hub.env (`BT_PHONE_MAC`)
- Runs as background daemon inside hub.py
- Arrival → executes welcome/home scene (configurable)
- Departure → executes leave/away scene
- Enables automatic presence management without manual NFC tags

Currently enabled; replaces manual `leave` scene trigger. Integrates with `presence.json` for state tracking.

---

## Claude Model Drift Audit (`audit.py`)

Independent longitudinal sampling system that detects model behaviour changes across Anthropic API updates.

**What it does:**
- Asks a fixed 30-question battery to Claude every 72 hours (scheduled at 03:00 via `tasks/anthropic_audit.py`)
- Compares each response against the immutable baseline run and the immediately preceding run
- Scores drift 0–10 per question; sends Telegram alerts if any question drifts > 6 or session average > 4
- Builds a permanent record of how Claude actually behaves independent of changelogs

**Storage:** `memory.db` tables:
- `anthropic_audit` — all runs (never truncated)
- `audit_baseline` — immutable baseline (SQL triggers prevent UPDATE/DELETE)
- `audit_model_transitions` — version change events
- `audit_meta` — battery SHA-256 hash, timestamps, last report

**Rules — non-negotiable:**
- **Never modify `audit_battery.json` after baseline.** File hash stored at baseline; any change triggers alert and blocks all subsequent runs.
- **Never modify `audit_baseline` table.** SQL triggers abort any UPDATE/DELETE — baseline is immutable anchor.
- **Never soften comparison engine system prompt.** Hardcoded adversarial framing in `audit.py` (not config) — "You are an auditor, not a defender" is deliberate.
- **Never truncate `anthropic_audit` table.** Runs from 18 months from now compared against baseline is the actual output of this system.

**Workflow:**
1. Fill in 30 question fields in `audit_battery.json`
2. Run `python3 audit.py --baseline` — runs all questions, stores immutable baseline, stores battery hash, enables 72h schedule
3. Scheduled runs happen automatically via `tasks/anthropic_audit.py` (daily at 03:00, internal 72h interval check)
4. Manual run: `python3 audit.py --force`
5. Infrastructure check: `python3 audit.py --dry-run`

**Alerts:** Telegram notification when drift_score > 6 on any question, session average > 4, or model version changes.

---

## Key Design Decisions & Gotchas

| Decision | Why |
|---|---|
| Hub proxies all `/lamp/*` to port 5000 | External clients only need one port (5001); scenes stay coordinated |
| Absolute TV volume: hammer KEY_VOLUMEDOWN ×60 then count up | Tizen has no volume-set API; this is reliable across all TV states |
| Beat sync is BPM-driven, not timestamp-driven | Spotify audio analysis API restricted to pre-Nov-2024 apps |
| `wiz-lamp.service` has 3s ExecStartPre sleep | Lamp UDP fails if network isn't settled at boot |
| Whisper semaphore in voice.py | Only one transcription at a time — prevents OOM on Pi |
| Voice pipeline uses no hardcoded phrases | All intents derived from
