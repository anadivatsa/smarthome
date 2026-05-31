#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Smart Home Hub — Install ==="

# 1. venv
if [ ! -d "$DIR/venv" ]; then
    echo "[1/3] Creating virtual environment..."
    python3 -m venv "$DIR/venv"
else
    echo "[1/3] Virtual environment exists, skipping."
fi

# 2. deps
echo "[2/3] Installing Python packages..."
"$DIR/venv/bin/pip" install --quiet --upgrade pip
"$DIR/venv/bin/pip" install --quiet -r "$DIR/requirements.txt"
echo "      Done."

# 3. systemd
echo "[3/3] Installing systemd service..."
sudo cp "$DIR/hub.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable hub
sudo systemctl restart hub
echo "      Service enabled and started."

echo ""
echo "=== Done ==="
echo "  curl http://localhost:5001/             # health check"
echo "  curl http://localhost:5001/tv/status    # TV info"
echo "  curl http://localhost:5001/scene/movie  # full scene test"
echo "  sudo journalctl -u hub -f               # live logs"
