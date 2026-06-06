#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Hey Neo Wake Word — Install ==="

echo "[1/5] Installing system packages (portaudio)..."
sudo apt-get install -y portaudio19-dev python3-dev

echo "[2/5] Creating virtual environment..."
if [ ! -d "$DIR/venv" ]; then
    python3 -m venv "$DIR/venv"
fi

echo "[3/5] Installing Python packages..."
"$DIR/venv/bin/pip" install --quiet --upgrade pip
"$DIR/venv/bin/pip" install --quiet -r "$DIR/requirements.txt"

echo "[4/5] Downloading Vosk small English model (~50 MB)..."
MODEL_DIR="$DIR/vosk-model-small-en-us"
if [ ! -d "$MODEL_DIR" ]; then
    cd "$DIR"
    wget -q --show-progress \
        https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.22.zip \
        -O vosk-model.zip
    unzip -q vosk-model.zip
    mv vosk-model-small-en-us-0.22 vosk-model-small-en-us
    rm vosk-model.zip
    echo "      Model ready."
else
    echo "      Model already present, skipping."
fi

echo "[5/5] Installing systemd service..."
sudo cp "$DIR/wakeword.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable wakeword

echo ""
echo "=== Done ==="
echo ""
echo "  NEXT STEPS:"
echo "  1. Edit wakeword/config.env — add your PORCUPINE_KEY"
echo "     Get a free key at: https://console.picovoice.ai"
echo ""
echo "  2. Train 'Hey Neo' at console.picovoice.ai → Wake Word → Train"
echo "     Platform: Raspberry Pi — download the .ppn file to this directory"
echo "     Then set WAKE_WORD_MODEL in config.env"
echo ""
echo "  3. sudo systemctl start wakeword"
echo "  4. sudo journalctl -u wakeword -f    # watch live"
echo ""
echo "  Say the wake word, then: 'movie time', 'next song', 'thunderstruck', etc."
