#!/usr/bin/env bash
# Install WiZ Lamp Controller: venv, dependencies, and systemd service.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE=wiz-lamp

echo "=== WiZ Lamp Controller — Install ==="
echo "Project directory: $DIR"
echo

# ── 1. Virtual environment ────────────────────────────────────────────────
if [ ! -d "$DIR/venv" ]; then
    echo "[1/4] Creating virtual environment ..."
    python3 -m venv "$DIR/venv"
else
    echo "[1/4] Virtual environment already exists, skipping."
fi

# ── 2. Install Python packages ────────────────────────────────────────────
echo "[2/4] Installing Python packages ..."
"$DIR/venv/bin/pip" install --quiet --upgrade pip
"$DIR/venv/bin/pip" install --quiet -r "$DIR/requirements.txt"
echo "      Done."

# ── 3. Discover lamp (optional, can be done later) ────────────────────────
echo
echo "[3/4] Lamp discovery"
LAMP_IP_CURRENT=$(grep -E '^LAMP_IP=' "$DIR/config.env" | cut -d= -f2 | tr -d ' ')
if [ -z "$LAMP_IP_CURRENT" ]; then
    read -rp "      Run lamp discovery now? [Y/n] " ANSWER
    ANSWER="${ANSWER:-y}"
    if [[ "$ANSWER" =~ ^[Yy] ]]; then
        "$DIR/venv/bin/python" "$DIR/discover_lamp.py"
    else
        echo "      Skipped. Edit config.env and set LAMP_IP before starting the service."
    fi
else
    echo "      LAMP_IP already set: $LAMP_IP_CURRENT"
fi

# ── 4. Install systemd service ────────────────────────────────────────────
echo
echo "[4/4] Installing systemd service ..."
sudo cp "$DIR/wiz-lamp.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE"
echo "      Service enabled (will start on next boot)."

# ── Done ──────────────────────────────────────────────────────────────────
echo
echo "=== Installation complete ==="
echo
echo "Commands:"
echo "  sudo systemctl start $SERVICE      # start now"
echo "  sudo systemctl status $SERVICE     # check status"
echo "  sudo journalctl -u $SERVICE -f     # live logs"
echo
echo "API (on port 5000 by default):"
printf "  %-30s %s\n" "curl http://localhost:5000/on"     "Turn on"
printf "  %-30s %s\n" "curl http://localhost:5000/off"    "Turn off"
printf "  %-30s %s\n" "curl http://localhost:5000/focus"  "Focus  (6500 K, 100%)"
printf "  %-30s %s\n" "curl http://localhost:5000/movie"  "Movie  (2700 K, 30%)"
printf "  %-30s %s\n" "curl http://localhost:5000/sleep"  "Sleep  (2200 K, 10%)"
printf "  %-30s %s\n" "curl http://localhost:5000/status" "Current state"
