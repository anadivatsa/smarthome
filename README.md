# Smart Home Hub

A Raspberry Pi–hosted HTTP API that ties together a Samsung TV and a WiZ smart lamp into a single, scriptable control surface. Scenes, NFC tags, volume ramps, lighting transitions, and deep-linked app launches — all triggered by a plain HTTP GET.

---

## Architecture

```
┌──────────────────────────────────────────────────┐
│               Raspberry Pi                        │
│                                                   │
│  ┌─────────────────────┐  ┌────────────────────┐  │
│  │   Smart Home Hub    │  │  WiZ Lamp Service  │  │
│  │   hub.py  :5001     │──│  app.py   :5000    │  │
│  └──────────┬──────────┘  └────────────────────┘  │
│             │  proxy /lamp/*                       │
└─────────────┼────────────────────────────────────┘
              │
    ┌─────────┴──────────┐
    │                    │
    ▼                    ▼
Samsung TV          WiZ Bulb
<TV_IP>              <LAMP_IP>
WebSocket :8002     UDP (pywizlight)
REST API  :8001
```

Two systemd services run on boot:

| Service | File | Port | Purpose |
|---|---|---|---|
| `hub.service` | `hub.py` | 5001 | Central orchestrator — scenes, TV, NFC, lamp proxy |
| `wiz-lamp.service` | `app.py` | 5000 | WiZ bulb controller — effects, transitions, static scenes |

The hub proxies all `/lamp/*` requests to the lamp service, so external clients only need to know about port 5001.

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

---

### WiZ Lamp Service (`app.py`)

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
├── hub.py          Central Flask API (port 5001)
├── tv.py           Samsung TV driver (samsungtvws + WoL)
├── scenes.json     Scene definitions (edit to add/change scenes)
├── tags.json       NFC tag → scene mappings (auto-updated at runtime)
├── requirements.txt
├── hub.service     systemd unit for hub.py
└── install.sh      Installs and enables hub.service

wiz-lamp/
├── app.py          WiZ lamp Flask API (port 5000)
├── discover_lamp.py Find the lamp IP on the local network
├── config.env      LAMP_IP and PORT
├── requirements.txt
├── wiz-lamp.service systemd unit for app.py
└── install.sh      Installs and enables wiz-lamp.service
```
