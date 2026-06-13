#!/usr/bin/env bash
# Installs the voice intelligence layer (voice.py) and voice.service.
# Reuses the existing smarthome venv — run hub install.sh first if venv doesn't exist.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Smart Home Voice Intelligence — Install ==="

# 1. System packages for PyAudio + build tools for webrtcvad
echo "[1/4] Installing system dependencies..."
sudo apt-get install -y -q \
    portaudio19-dev \
    python3-dev \
    build-essential \
    ffmpeg
echo "      Done."

# 2. Reuse (or create) the hub venv
if [ ! -d "$DIR/venv" ]; then
    echo "[2/4] Creating virtual environment..."
    python3 -m venv "$DIR/venv"
else
    echo "[2/4] Using existing virtual environment."
fi

# 3. Python packages
echo "[3/4] Installing Python packages..."
"$DIR/venv/bin/pip" install --quiet --upgrade pip
"$DIR/venv/bin/pip" install --quiet \
    openai-whisper \
    webrtcvad \
    pyaudio \
    anthropic
echo "      Done."

# 4. voice.env + systemd service
echo "[4/4] Installing voice.service..."

if [ ! -f "$DIR/voice.env" ]; then
    cp "$DIR/voice.env.example" "$DIR/voice.env"
    echo ""
    echo "  *** voice.env created from example ***"
    echo "  Edit $DIR/voice.env and set ANTHROPIC_API_KEY before starting the service."
    echo ""
fi

sudo cp "$DIR/voice.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable voice
echo "      Service installed and enabled (not yet started)."

echo ""
echo "=== Done ==="
echo ""
echo "  Next steps:"
echo "  1. nano ~/smarthome/voice.env          # add your ANTHROPIC_API_KEY"
echo "  2. sudo systemctl start voice"
echo "  3. sudo journalctl -u voice -f         # live logs"
echo ""
echo "  Tune VAD aggressiveness in voice.env if you get false triggers (VAD_MODE=3)"
echo "  or missed wake-ups (VAD_MODE=1)."
