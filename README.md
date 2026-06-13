# Smart Home Hub

A Raspberry Pi–hosted HTTP API that ties together a Samsung TV and a WiZ smart lamp into a single, scriptable control surface. Scenes, NFC tags, volume ramps, lighting transitions, and deep-linked app launches — controlled via HTTP, Telegram text/voice, or NFC tap. Includes persistent vector memory, a Telegram bot with conversation history, and a lightweight scheduled task system.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Raspberry Pi (Neo)                       │
│                                                              │
│  tgvoice.py ── Telegram bot ── Whisper + Claude + memory    │
│  voice.py   ── local mic VAD ─ Whisper + Claude             │
│                      │                                       │
│  ┌───────────────────▼──────────┐  ┌──────────────────────┐ │
│  │   hub.py  :5001              │  │  wiz-lamp/app.py     │ │
│  │   scenes / TV / Spotify /    │──│  :5000               │ │
│  │   NFC / presence / memory    │  │  effects/transitions │ │
│  └───────────────────┬──────────┘  └──────────────────────┘ │
│                      │ proxy /lamp/*                         │
│  memory.py ── SQLite + fastembed (vector search)             │
│  scheduler.py ── APScheduler background thread               │
└──────────────────────┼──────────────────────────────────────┘
                       │
             ┌─────────┴──────────┐
             ▼                    ▼
       Samsung TV             WiZ Bulb
       WebSocket :8002        UDP (pywizlight)
       REST API  :8001
```

Services running on boot:

| Service | File | Port | Purpose |
|---|---|---|---|
| `hub.service` | `hub.py` | 5001 | Central orchestrator — scenes, TV, NFC, memory API, lamp proxy |
| `wiz-lamp.service` | `wiz-lamp/app.py` | 5000 | WiZ bulb controller — effects, transitions, static scenes |
| `tgvoice.service` | `tgvoice.py` | — | Telegram bot with Whisper transcription and conversation memory |
| `voice.service` | `voice.py` | — | Local mic VAD → Whisper → Claude intent pipeline |
| `bayern-notifier.service` | `bavaria_notifier.py` | — | Bayern Munich goal/match Telegram notifications |

The hub proxies all `/lamp/*` requests to the lamp service, so external clients only need port 5001.

---

## Hardware

| Device | Role | Address |
|---|---|---|
| Raspberry Pi | Runs both services 24/7 | — |
| Samsung TV (Tizen) | Controlled via WebSocket + REST | `<TV_IP>` |
| WiZ smart bulb | Controlled via UDP (pywizlight) | `<LAMP_IP>` |
| NFC tags (optional) | Tap-to-scene triggers | — |

---

## Services

### Smart Home Hub (`hub.py`)

Central Flask API on port 5001. Handles scenes, TV control, NFC tags, and proxies to the lamp service.

#### Scenes

A scene sets the lamp and TV state in parallel. Trigger with `GET /scene/<name>`.

| Scene | Lamp | TV |
|---|---|---|
| `movie` | Warm white 30% | On → Prime Video, vol 15 |
| `netflix` | Warm white 30% | On → Netflix, vol 15, auto-confirm profile |
| `youtube` | Morning light | On → YouTube deep-link playlist, vol ramp 20→60 over 2 min |
| `music` | Pulse effect | On → Spotify, vol ramp 20→60 over 2 min |
| `gaming` | Vivid blue 60% | On, vol 18 |
| `morning` | Wake transition | On → YouTube, vol 8 |
| `focus` | Cool white 100% | Off |
| `relax` | Soft amber 40% | On, vol 10 |
| `reading` | Neutral white 80% | Off |
| `dinner` | Candlelight 50% | Off |
| `romance` | Deep warm red 15% | Off |
| `party` | Party colour cycle | On → Spotify, vol 20 |
| `sunset` | Sunset transition | Off |
| `goodnight` | Goodnight fade | Off |
| `off` | Off | Off |

Scene definitions live in `scenes.json` — add or edit scenes without touching Python.

**Scene config fields (tv block):**

```json
{
  "action":      "on | off",
  "volume":      15,           // relative delta (steps up/down)
  "volume_abs":  20,           // absolute level 0–100 (zeroes out first)
  "app":         "youtube",    // app to launch
  "playlist":    "https://...",// deep link passed to the app
  "post_launch": {             // key sequence fired after app opens
    "delay": 5,
    "keys":  ["KEY_ENTER"]
  },
  "volume_ramp": {             // gradual volume increase in background
    "to":    60,
    "over":  120,
    "delay": 8
  }
}
```

#### TV control

| Endpoint | Action |
|---|---|
| `GET /tv/on` | Power on (WoL if fully off, KEY_POWER if standby, no-op if already on) |
| `GET /tv/off` | Power off |
| `GET /tv/status` | Power state, model, OS |
| `GET /tv/mute` | Toggle mute |
| `GET /tv/volume/<n>` | Relative volume (positive = up, negative = down) |
| `GET /tv/source/<name>` | Switch input: `hdmi1` `hdmi2` `hdmi3` `hdmi4` `tv` `av` |
| `GET /tv/app/<name>` | Launch app: `netflix` `youtube` `prime` `spotify` `appletv` |
| `GET /tv/play\|pause\|stop\|ff\|rewind\|next\|prev` | Playback |
| `GET /tv/home\|back\|up\|down\|left\|right\|enter` | Navigation |
| `GET /tv/key/<KEY_CODE>` | Send any raw Samsung key code |

The volume ramp runs in a background thread and includes a watchdog: it polls TV status every 4 seconds and stops itself if the TV is powered off or enters standby externally, so it never fights a physical remote or another app.

#### NFC tags

| Endpoint | Action |
|---|---|
| `GET /tag/<uid>` | Tap — executes the scene registered to this tag |
| `GET /tag/<uid>/<scene>` | Register a tag UID to a scene |
| `GET /tags` | List all registered tags and available scenes |

Tag mappings are stored in `tags.json`. UIDs are normalised (uppercase, colons stripped), so any NFC reader format works.

#### Lamp proxy

`GET /lamp/<endpoint>` — transparently proxies to the lamp service on port 5000. Lets clients use a single base URL for everything.

#### Memory API endpoints

| Endpoint | Description |
|---|---|
| `GET /api/memory?q=<query>&n=5` | Semantic search over long-term memory |
| `GET /api/scene_log?n=20` | Scene activation history with trigger source |

Both endpoints require the `NEO_API_KEY`. Results are JSON and power future dashboard UI.

---

### Telegram Bot (`tgvoice.py`)

Telegram bot running as `tgvoice.service`. Accepts text and voice messages from the allowlisted chat ID. Voice messages are transcribed with Whisper (local, offline). All commands go through Claude for intent recognition and then dispatch to the hub.

**Conversation memory:** every message is stored in `memory.db`. The last 15 turns are sent as context to Claude on each request, so Neo remembers what you said earlier in the session. Relevant long-term memories are also retrieved and injected into the system prompt.

**Commands:**

| Command | Action |
|---|---|
| `/status` | Live service health + lamp, TV, Spotify, presence state |
| `/memory <query>` | Search long-term memory, return top 5 results |
| `/scene_history` | Last 10 scene activations with trigger source and time |
| `/remember <text>` | Manually store something as a long-term memory |
| `/forget` | Clear conversation history (long-term memories kept) |
| `/repair <task>` | Run a scheduled task, ask Claude to fix any errors, preview fix |
| `/confirm` | Apply a pending `/repair` fix and re-run the task |
| `/cancel` | Discard a pending `/repair` fix |

**`/repair` self-repair loop:** if a task file fails, `/repair` captures the error, sends it to Claude with the file contents, receives corrected code, shows a preview in Telegram, and waits for `/confirm` before overwriting. A timestamped backup is made before any file is changed.

---

### Memory Store (`memory.py`)

Persistent SQLite database at `data/memory.db`. Used by `tgvoice.py` for conversation history and by `hub.py` for scene event logging.

**Embedding backend (auto-detected at startup, three-tier fallback):**

1. `fastembed` BAAI/bge-small-en-v1.5 (384-dim, runs on Pi CPU) + `sqlite-vec` → KNN vector search
2. `fastembed` + `numpy` cosine similarity → in-process vector search
3. SQLite FTS5 → full-text search fallback

The model loads in a background thread on startup (~45 MB download on first run). FTS5 is used until the model is ready — no blocking, no crashes.

**Tables:**

| Table | Purpose |
|---|---|
| `memories` | Long-term storage — manually stored notes, ingested documents |
| `memories_fts` | FTS5 index on `memories` (auto-maintained via triggers) |
| `conversation` | Rolling conversation history, max 1000 turns (auto-trimmed) |
| `scene_log` | Every scene activation: scene name, trigger source, timestamp |
| `sensor_log` | Future GPIO / sensor readings |

**Public API:**

```python
memory.init()                               # create tables, start background loader
memory.store_conversation(role, content)    # append a turn
memory.get_recent(n=15)                     # last n turns as [{role, content}] for Claude
memory.store_memory(content, role, source)  # embed and persist a long-term memory
memory.search(query, n=5)                   # semantic or FTS search
memory.store_scene_event(scene, triggered_by)  # log scene activation
memory.get_scene_history(scene=None, n=20)     # retrieve scene log
memory.ingest_url(url)                      # fetch, chunk, embed, store a webpage
memory.ingest_pdf(path)                     # extract, chunk, embed, store a PDF
memory.prune_conversation(days=30)          # delete old conversation rows
```

---

### Scheduled Tasks (`scheduler.py` + `tasks/`)

`scheduler.py` loads Python files from `tasks/` at startup and runs them on a cron schedule using APScheduler. It runs as a daemon background thread inside `hub.py` — if the scheduler fails it never takes down the hub.

**Task file format** — header comment block at the top of each `.py` file:

```python
# SCHEDULE: daily at 07:30
# ENABLED: false
# DESCRIPTION: Send morning briefing to Telegram
```

Supported schedule expressions:
- `daily at HH:MM`
- `weekly on <weekday> at HH:MM`

A task that fails 3 consecutive times is automatically disabled (`ENABLED` set to `false` in the file header) and a warning is logged.

**Built-in tasks (all disabled by default — set `ENABLED: true` to activate):**

| Task | Schedule | Description |
|---|---|---|
| `tasks/morning_brief.py` | Daily 07:30 | Telegram message with date, Bayern fixture, uptime, last 3 scenes, weather |
| `tasks/disk_check.py` | Daily 09:00 | Alert if root partition exceeds 85% |
| `tasks/memory_cleanup.py` | Sunday 03:00 | Delete conversation rows older than 30 days |

**Enabling a task:**

```bash
# Edit the header in the task file
sed -i 's/# ENABLED: false/# ENABLED: true/' tasks/morning_brief.py
sudo systemctl restart hub
```

---

### WiZ Lamp Service (`wiz-lamp/app.py`)

Flask API on port 5000. Communicates with the WiZ bulb directly over UDP using `pywizlight`.

#### Static scenes

| Endpoint | Colour temp | Brightness |
|---|---|---|
| `GET /on` | Default | 100% |
| `GET /off` | — | — |
| `GET /focus` | 6500 K cool white | 100% |
| `GET /morning` | 5000 K daylight | 70% |
| `GET /reading` | 4000 K neutral | 80% |
| `GET /relax` | 2400 K amber | 40% |
| `GET /dinner` | 2500 K candlelight | 50% |
| `GET /movie` | 2700 K warm | 30% |
| `GET /sleep` | 2200 K very warm | 10% |
| `GET /romance` | RGB deep red | 15% |
| `GET /gaming` | RGB blue-white | 60% |
| `GET /brightness/<0-100>` | Current | Set % |

#### Looping effects (run until `/off` or another command)

| Endpoint | Description |
|---|---|
| `GET /blink` | Fast on/off flash |
| `GET /pulse` | Slow breathing dim↔bright |
| `GET /party` | Random colour cycling |
| `GET /alert` | Red SOS flash pattern |
| `GET /strobe` | Fast white strobe |
| `GET /candle` | Warm candle flicker |
| `GET /campfire` | Intense fire flicker |
| `GET /aurora` | Slow northern-lights colour drift |

#### Transitions (run once, settle at final state)

| Endpoint | Description | Duration |
|---|---|---|
| `GET /wake` | Dim warm → full daylight | ~90 s |
| `GET /bedtime` | Medium → warm dim sleep level | ~2 min |
| `GET /fade` | Current → off | ~30 s |
| `GET /sunrise` | Deep red → orange → warm white | ~5 min |
| `GET /sunset` | Golden → deep red → off | ~5.5 min |
| `GET /goodnight` | Long peaceful fade to off | ~5.5 min |

The effect engine runs each effect in a background thread. Any new command cancels the current effect first. A watchdog thread polls the bulb every 3 seconds and stops the effect automatically if an external device turns the lamp off.

---

## Installation

```bash
# 1. Clone and install dependencies
git clone https://github.com/anadivatsa/smarthome
cd smarthome
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Discover your WiZ lamp IP
cd ../wiz-lamp
python discover_lamp.py
# → edit config.env: LAMP_IP=<discovered IP>

# 3. Install systemd services
sudo bash install.sh          # smarthome hub
cd ../wiz-lamp && sudo bash install.sh   # lamp service

# 4. First TV pairing
# Trigger any TV command — the TV will prompt you to allow "PiHub"
# Accept it; the token is saved to ~/.smarthome/tv_token.json
curl http://localhost:5001/tv/status
```

---

## Possible Usages & Extensions

### Home automation triggers
- **NFC tags** on furniture, door frames, or remotes: tap to switch scenes instantly without unlocking a phone.
- **Cron jobs** on the Pi: `curl localhost:5001/scene/morning` at 7 am, `scene/goodnight` at 11 pm.
- **Webhook receiver**: add a `/webhook` route to trigger scenes from IFTTT, Home Assistant, or any HTTP-capable service.

### Voice control
Expose the hub through a reverse proxy (e.g. nginx + Let's Encrypt) and wire it to:
- **Siri Shortcuts** — one-tap or "Hey Siri, movie time"
- **Google Assistant** via IFTTT webhooks
- A local **Whisper** speech-to-command pipeline on the Pi itself

### Phone widgets / shortcuts
- iOS Shortcuts / Android Tasker HTTP actions hitting `GET /scene/<name>`
- A home-screen widget grid of scene buttons (no app required — just bookmarked URLs in a browser)

### Presence-based automation
- Combine with `bt_pair.py` (already on the Pi): detect when your phone's Bluetooth is in range to trigger a "welcome home" scene, and trigger "off" when you leave.

### Morning routine
The `morning` scene already ramps light and starts YouTube. Extend it:
- Add a `GET /tv/volume/<n>` call on a timer to gradually increase volume
- Chain with a smart plug (via a similar UDP/REST controller) to start a coffee maker

### Sleep timer
Hit `GET /scene/goodnight` from bed — lamp fades over 5.5 minutes, TV turns off. Add a query param like `/scene/goodnight?tv_delay=30` to give the TV 30 extra minutes if you want to finish an episode.

### Party mode
`GET /scene/party` starts lamp colour cycling and Spotify. Extend `scenes.json` to also trigger smart plugs on a disco ball or LED strips via the same proxy pattern the lamp uses.

### Security / alert
`GET /lamp/alert` fires the red SOS pattern. Wire this to a motion sensor, a door sensor, or a Telegram bot command to flash an alert when triggered remotely.

### Multi-room
The hub pattern (scenes.json + parallel threads) scales to more devices. Add a second lamp, a soundbar, or smart plugs by following the same proxy pattern in `hub.py` — each device gets its own service on a new port, proxied under a new path prefix.

---

## File reference

```
smarthome/
├── hub.py               Central Flask API (port 5001)
├── tv.py                Samsung TV driver (samsungtvws + WoL)
├── spotify.py           Spotify Web API wrapper (OAuth, playback)
├── beat_sync.py         BPM-driven lamp pulse synced to Spotify
├── auth.py              API key auth (before_request hook)
├── memory.py            Vector memory store (SQLite + fastembed/FTS5)
├── scheduler.py         APScheduler background task runner
├── backup.py            Timestamped file backup utility
├── tgvoice.py           Telegram bot (Whisper + Claude + memory)
├── voice.py             Local mic VAD → Whisper → Claude pipeline
├── bavaria_notifier.py  Bayern Munich goal/match Telegram notifier
├── scenes.json          Scene definitions (edit to add/change scenes)
├── tags.json            NFC tag → scene mappings (auto-updated)
├── .env.example         Template for all environment variables
├── hub.env.example      Template for hub-specific env vars
├── requirements.txt
├── hub.service          systemd unit for hub.py
│
├── tasks/
│   ├── morning_brief.py   Daily 07:30 — Telegram morning briefing
│   ├── disk_check.py      Daily 09:00 — disk usage alert
│   └── memory_cleanup.py  Sunday 03:00 — prune old conversations
│
└── data/                  Runtime data (gitignored)
    ├── memory.db          SQLite database (conversation + memories + logs)
    └── backups/           Pre-repair file backups (timestamped)

wiz-lamp/
├── app.py               WiZ lamp Flask API (port 5000)
├── discover_lamp.py     Network discovery helper
├── config.env           LAMP_IP and PORT
├── wiz-lamp.service     systemd unit
└── install.sh           Installs and enables wiz-lamp.service
```

---

## Security

### API key authentication

All hub endpoints require a `NEO_API_KEY` unless the key is unset (open mode for initial setup). Set the key in `smarthome/hub.env`:

```bash
# Generate a strong key
python3 -c "import secrets; print('NEO_API_KEY=' + secrets.token_urlsafe(32))" \
  > /home/anadivatsa/smarthome/hub.env
chmod 600 /home/anadivatsa/smarthome/hub.env
sudo systemctl restart hub tgvoice voice
```

**Clients send the key via:**

| Client | Method |
|---|---|
| curl / scripts | `X-Neo-Key: <key>` header |
| Siri Shortcuts | Append `?key=<key>` to the URL |
| NFC automations | Append `?key=<key>` to the URL |
| Dashboard | Prompted on first visit; stored in `sessionStorage` |
| tgvoice / voice | Auto-read from `hub.env` via systemd `EnvironmentFile` |

**Public endpoints (never require a key):**

- `GET /` — dashboard HTML
- `GET /spotify/auth` — OAuth initiation
- `GET /spotify/callback` — Spotify OAuth redirect
- `GET /spotify/exchange` — Headless code exchange
- `GET /shortcuts` — Siri Shortcuts reference page

### Rotating the key

```bash
python3 -c "import secrets; print('NEO_API_KEY=' + secrets.token_urlsafe(32))" \
  > /home/anadivatsa/smarthome/hub.env
sudo systemctl restart hub tgvoice voice
# Then update ?key= in all Siri Shortcuts and NFC automations
```

### GET → POST migration note

All state-changing routes now accept both `GET` and `POST`. Existing Siri Shortcuts and NFC automations continue to work unchanged via GET. GET responses carry an `X-Deprecated` header as a reminder to migrate. To suppress the header, switch your client to `POST`.

### Tailscale / network exposure warning

The hub binds to `0.0.0.0:5001` — reachable by anything on your LAN. The API key protects against unauthorized control from other LAN devices, but the hub should **never be directly exposed to the internet** without an additional auth layer (Tailscale, Nginx + mTLS, or similar).

If you access Neo remotely, use [Tailscale](https://tailscale.com/): install it on both the Pi and your phone, then access `http://100.x.x.x:5001` over the Tailscale private network. The API key remains required even over Tailscale.

### Credential file locations

| File | Contains | Gitignored |
|---|---|---|
| `smarthome/hub.env` | `NEO_API_KEY` | ✅ (`*.env`) |
| `smarthome/voice.env` | `ANTHROPIC_API_KEY`, model config | ✅ |
| `smarthome/spotify.env` | Spotify client ID/secret | ✅ |
| `smarthome/notifier.env` | Telegram bot token + chat ID | ✅ |
| `wiz-lamp/config.env` | Lamp IP | ✅ |
| `~/.smarthome/tv_token.json` | Samsung TV pairing token | Outside repo |
| `smarthome/spotify_tokens.json` | Spotify OAuth tokens | ✅ |

See `.env.example` for a full template with all variable names.
