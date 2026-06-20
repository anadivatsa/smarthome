#!/data/data/com.termux/files/usr/bin/bash
# Neo mic node setup — run this once inside Termux on your Android device.
# Usage: curl http://192.168.1.8:5001/termux/install | bash

set -e

# ── Guard: must be running inside Termux ─────────────────────────────────────
if [[ -z "$TERMUX_VERSION" && ! -d "/data/data/com.termux" ]]; then
    echo "ERROR: This script must be run inside Termux on Android."
    exit 1
fi

echo ""
echo "══════════════════════════════════════════"
echo "  Neo mic node — Termux setup"
echo "══════════════════════════════════════════"
echo ""

# ── 1. Install packages ───────────────────────────────────────────────────────
echo "→ Installing packages (termux-api, python)…"
pkg install -y termux-api python 2>&1 | tail -5

echo "→ Installing Python deps…"
pip install -q requests

# ── 2. Download neo_mic.py ────────────────────────────────────────────────────
echo "→ Downloading neo_mic.py from Neo…"
curl -s http://192.168.1.8:5001/termux/neo_mic -o "$HOME/neo_mic.py"
if [[ ! -s "$HOME/neo_mic.py" ]]; then
    echo "ERROR: Failed to download neo_mic.py — is Neo reachable on 192.168.1.8:5001?"
    exit 1
fi
echo "   Saved to ~/neo_mic.py"

# ── 3. Write config ───────────────────────────────────────────────────────────
echo "→ Writing ~/neo.env…"
cat > "$HOME/neo.env" <<'ENVEOF'
NEO_HUB_URL=http://192.168.1.8:5001
NEO_API_KEY=QbWBj9LS58rQSueXAAI6eWm2Xrx6gAIU_okG7-53n9c
CHUNK_SEC=4
SAMPLE_RATE=16000
ENVEOF

# ── 4. Write start / stop helpers ─────────────────────────────────────────────
echo "→ Writing ~/start_neo.sh and ~/stop_neo.sh…"

cat > "$HOME/start_neo.sh" <<'STARTEOF'
#!/data/data/com.termux/files/usr/bin/bash
PID_FILE="$HOME/neo_mic.pid"
LOG_FILE="$HOME/neo_mic.log"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Already running (PID $(cat "$PID_FILE")). Run ~/stop_neo.sh first."
    exit 1
fi

set -a; source "$HOME/neo.env"; set +a
nohup python "$HOME/neo_mic.py" >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "Neo mic started (PID $!). Logs: $LOG_FILE"
STARTEOF

cat > "$HOME/stop_neo.sh" <<'STOPEOF'
#!/data/data/com.termux/files/usr/bin/bash
PID_FILE="$HOME/neo_mic.pid"
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    kill "$(cat "$PID_FILE")"
    rm -f "$PID_FILE"
    echo "Neo mic stopped."
else
    echo "Not running."
fi
STOPEOF

chmod +x "$HOME/start_neo.sh" "$HOME/stop_neo.sh"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════"
echo "  Setup complete!"
echo "══════════════════════════════════════════"
echo ""
echo "  NEXT STEPS:"
echo ""
echo "  1. Install Termux:API companion app from F-Droid"
echo "     (search 'Termux API' — must be F-Droid version, not Play Store)"
echo ""
echo "  2. In Android Settings → Apps → Termux:API"
echo "     → Permissions → Microphone → Allow"
echo ""
echo "  3. Start the mic node:"
echo "     ~/start_neo.sh"
echo ""
echo "  4. Speak — Neo will respond. View logs:"
echo "     tail -f ~/neo_mic.log"
echo ""
echo "  To stop: ~/stop_neo.sh"
echo ""
