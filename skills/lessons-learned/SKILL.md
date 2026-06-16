# Lessons Learned

> **Read this before debugging anything.**
> Append a new entry every time you fix a bug — don't just fix and move on.
> Format: `## YYYY-MM-DD — [short title]` → what broke → root cause → fix.

---

## 2026-05 — GPU torch bloating venv by 4.7 GB

**What broke:** `pip install openai-whisper` pulled in a full CUDA-enabled PyTorch build, expanding the venv from ~200 MB to ~5 GB on the Pi.

**Root cause:** `openai-whisper` lists `torch` as a dependency without a CPU-only pin. On Linux/ARM, pip resolves the default PyTorch wheel which includes GPU binaries — even on a Pi that has no GPU.

**Fix:** Install CPU-only torch first, before openai-whisper, using the PyTorch CPU wheel index:
```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install openai-whisper
```
This keeps torch around 200 MB instead of 4.7 GB.

---

## 2026-05 — PortAudio missing on Termux

**What broke:** `pip install sounddevice` succeeded but `import sounddevice` crashed at runtime with a missing shared library error for PortAudio.

**Root cause:** `sounddevice` is a Python wrapper around PortAudio. On Termux, the native PortAudio library must be installed separately via the Termux package manager — pip only installs the Python bindings.

**Fix:**
```bash
pkg install portaudio
pip install sounddevice
```
Always install the native lib before the Python wrapper on Termux.

---

## 2026-05 — Termux mic access failing with OpenSLES error -9999

**What broke:** `termux-microphone-record` returned error -9999 (OpenSLES). Microphone was inaccessible from Termux even after granting mic permission.

**Root cause:** Two separate steps are required that aren't obvious:
1. The `Termux:API` *Android app* must be installed (from F-Droid), not just the `termux-api` pkg inside Termux. The pkg provides the CLI commands; the Android app is the bridge that actually accesses hardware.
2. Mic permission must be granted to **both** `Termux` and `Termux:API` separately in Android Settings → Apps → Permissions.

**Fix:**
1. Install Termux:API app from F-Droid (not Play Store).
2. `pkg install termux-api` inside Termux.
3. Grant microphone permission to both Termux and Termux:API in Android app permissions.

---

## 2026-05 — AudioRelay abandoned (alpha Linux ARM build, unreliable)

**What broke:** AudioRelay was explored as a way to stream mic audio from Android to Pi. The Linux ARM build was in alpha, dropped connections frequently, and required manual reconnection after every phone sleep cycle.

**Root cause:** AudioRelay's Linux ARM support was not production-ready. Not a configuration issue — the build itself was unstable.

**Fix:** Abandoned entirely. Replaced with Telegram voice bot approach (`tgvoice.py`): user sends a voice message to the Telegram bot, which transcribes via Whisper and acts. More reliable, works over any network, no persistent connection required.

---

## 2026-05 — TV_IP mismatch undetected for weeks

**What broke:** All TV commands silently failed or timed out. The hub appeared to work (no exceptions surfaced to the API caller) but the TV never responded.

**Root cause:** `TV_IP` in `.env` was set to `192.168.1.16` (a leftover from an earlier network config). The TV's actual IP is `192.168.1.2`. The mismatch sat undetected because `samsungtvws` connection failures were swallowed without raising in the scene runner, and no integration test exercised the TV path.

**Fix:** Corrected `TV_IP=192.168.1.2` in `.env`. Added the TV IP to the Hardware table in CLAUDE.md so it's always visible. Lesson: verify device IPs at the router whenever network config changes.

---

## 2026-06 — voice.py crash-looping 762 times before being noticed

**What broke:** `voice.service` restarted 762 times over several hours. journalctl showed `OSError: [Errno -9996] Invalid input device` on every launch.

**Root cause:** `voice.py` had a hardcoded PyAudio device index that matched the correct USB mic on the first install but became wrong after a Pi reboot changed device enumeration order.

**Fix:** Changed `voice.py` to enumerate PyAudio devices at startup and select by name substring match rather than hardcoded index. Also added `RestartSec=10` and `StartLimitIntervalSec=300` / `StartLimitBurst=5` to `voice.service` to prevent runaway restart loops burning CPU before anyone notices.
