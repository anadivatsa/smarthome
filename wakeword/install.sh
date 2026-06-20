#!/usr/bin/env bash
# Hey Neo Wake Word — Install & Train
# Trains a custom "Hey Neo" openWakeWord model from synthetic TTS data.
# No API keys required. Re-run to retrain (add --retrain flag).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RETRAIN=false
[[ "${1:-}" == "--retrain" ]] && RETRAIN=true

echo "=== Hey Neo Wake Word — Install & Train ==="

# ---------------------------------------------------------------------------
echo "[1/9] Installing system packages..."
# ---------------------------------------------------------------------------
sudo apt-get install -y --no-install-recommends \
    portaudio19-dev python3-dev libopenblas-dev git unzip wget

# ---------------------------------------------------------------------------
echo "[2/9] Creating virtual environment..."
# ---------------------------------------------------------------------------
if [ ! -d "$DIR/venv" ]; then
    python3 -m venv "$DIR/venv"
fi
PIP="$DIR/venv/bin/pip"
PY="$DIR/venv/bin/python"

"$PIP" install --quiet --upgrade pip

# ---------------------------------------------------------------------------
echo "[3/9] Installing runtime Python packages..."
# ---------------------------------------------------------------------------
# openwakeword 0.6.0 is installed with --no-deps because its wheel metadata
# incorrectly requires tflite-runtime (no cp313/aarch64 wheel exists). The
# runtime inference uses onnxruntime instead via inference_framework="onnx".
"$PIP" install --quiet --no-deps openwakeword==0.6.0
"$PIP" install --quiet \
    "onnxruntime>=1.20.0,<2" \
    "numpy>=1.24.0,<3" \
    "vosk>=0.3.45" \
    "pyaudio>=0.2.14" \
    "requests>=2.31.0" \
    "python-dotenv>=1.0.0" \
    scipy \
    scikit-learn

# ---------------------------------------------------------------------------
echo "[4/9] Downloading Vosk small English model (~50 MB)..."
# ---------------------------------------------------------------------------
MODEL_DIR="$DIR/vosk-model-small-en-us"
if [ ! -d "$MODEL_DIR" ]; then
    cd "$DIR"
    wget -q --show-progress \
        https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip \
        -O vosk-model.zip
    unzip -q vosk-model.zip
    mv vosk-model-small-en-us-0.15 vosk-model-small-en-us
    rm vosk-model.zip
    echo "      Vosk model ready."
else
    echo "      Vosk model already present, skipping."
fi

# Skip remaining steps if model already exists and --retrain not requested
if [ -f "$DIR/hey_neo.onnx" ] && [ "$RETRAIN" = false ]; then
    echo ""
    echo "  hey_neo.onnx already exists. Skipping training."
    echo "  Use --retrain to force a rebuild."
    echo ""
    echo "[9/9] Ensuring systemd service is installed..."
    sudo cp "$DIR/wakeword.service" /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable wakeword
    echo ""
    echo "=== Done (model cached) ==="
    echo "  sudo systemctl start wakeword"
    echo "  sudo journalctl -u wakeword -f"
    exit 0
fi

# ---------------------------------------------------------------------------
echo "[5/9] Installing training dependencies..."
# ---------------------------------------------------------------------------
# torch is large (~700 MB wheel); pip's default /tmp tmpfs (3.9 GB RAM-backed)
# can fill up on first download. Use main-filesystem scratch dir instead.
mkdir -p "$DIR/tmp"
TMPDIR="$DIR/tmp" "$PIP" install --quiet \
    piper-sample-generator \
    torch \
    torchaudio \
    speechbrain \
    audiomentations \
    torch-audiomentations \
    mutagen \
    pyyaml \
    "datasets<3" \
    webrtcvad-wheels \
    tqdm \
    torchinfo \
    torchmetrics \
    pronouncing \
    acoustics
rm -rf "$DIR/tmp"

# acoustics 0.2.6 references scipy.special.sph_harm which was removed in scipy 1.15.
# Patch its __init__.py to skip the broken directivity submodule (unused by openwakeword).
ACOUSTICS_INIT=$("$PY" -c "import acoustics, os; print(os.path.join(os.path.dirname(acoustics.__file__), '__init__.py'))" 2>/dev/null || true)
if [ -n "$ACOUSTICS_INIT" ] && grep -q "^import acoustics.directivity" "$ACOUSTICS_INIT" 2>/dev/null; then
    sed -i 's/^import acoustics\.directivity$/try:\n    import acoustics.directivity\nexcept ImportError:\n    pass  # sph_harm removed in scipy 1.15/' "$ACOUSTICS_INIT"
fi

# ---------------------------------------------------------------------------
echo "[6/9] Downloading Piper voice models for TTS diversity (~180 MB total)..."
# ---------------------------------------------------------------------------
VOICES_DIR="$DIR/training/voices"
mkdir -p "$VOICES_DIR"

download_voice() {
    local name="$1" base_url="$2"
    if [ ! -f "$VOICES_DIR/${name}.onnx" ]; then
        echo "      Downloading ${name}..."
        wget -q --show-progress -O "$VOICES_DIR/${name}.onnx"      "${base_url}/${name}.onnx?download=true"
        wget -q                 -O "$VOICES_DIR/${name}.onnx.json"  "${base_url}/${name}.onnx.json?download=true"
    else
        echo "      ${name} already present."
    fi
}

HF_VOICES="https://huggingface.co/rhasspy/piper-voices/resolve/main"
download_voice "en_US-lessac-medium"  "${HF_VOICES}/en/en_US/lessac/medium"
download_voice "en_US-ryan-medium"    "${HF_VOICES}/en/en_US/ryan/medium"
download_voice "en_GB-alan-medium"    "${HF_VOICES}/en/en_GB/alan/medium"

echo "[6/9] Downloading openWakeWord validation features (~176 MB)..."
TRAIN_DIR="$DIR/training"
mkdir -p "$TRAIN_DIR"
if [ ! -f "$TRAIN_DIR/validation_set_features.npy" ]; then
    wget -q --show-progress \
        "https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/validation_set_features.npy" \
        -O "$TRAIN_DIR/validation_set_features.npy"
fi

# Generate minimal background audio (silence) so the training pipeline has something
mkdir -p "$TRAIN_DIR/background"
"$PY" -c "
import numpy as np, scipy.io.wavfile, os
p = '${TRAIN_DIR}/background/silence.wav'
if not os.path.exists(p):
    scipy.io.wavfile.write(p, 16000, np.zeros(16000*30, dtype=np.int16))
"

# ---------------------------------------------------------------------------
echo "[7/9] Writing training config..."
# ---------------------------------------------------------------------------
# openwakeword expects a generate_samples.py file on sys.path; the pip package
# puts the function in __main__.py. Create a shim in TRAIN_DIR so the import works.
if [ ! -f "$TRAIN_DIR/generate_samples.py" ]; then
    echo "from piper_sample_generator.__main__ import generate_samples" > "$TRAIN_DIR/generate_samples.py"
fi
PSG_PATH="$TRAIN_DIR"

# piper_sample_generator.__main__ imports piper_train.vits.commons (VITS math utils)
# which is not on PyPI. Create a minimal stub with the two functions it uses.
SITE_PKGS=$("$PY" -c "import site; print(site.getsitepackages()[0])")
PT_COMMONS="$SITE_PKGS/piper_train/vits/commons.py"
if [ ! -f "$PT_COMMONS" ]; then
    mkdir -p "$SITE_PKGS/piper_train/vits"
    touch "$SITE_PKGS/piper_train/__init__.py" "$SITE_PKGS/piper_train/vits/__init__.py"
    cat > "$PT_COMMONS" << 'PYEOF'
import torch
import torch.nn.functional as F

def sequence_mask(length, max_length=None):
    if max_length is None:
        max_length = length.max()
    x = torch.arange(max_length, dtype=length.dtype, device=length.device)
    return x.unsqueeze(0) < length.unsqueeze(1)

def generate_path(duration, mask):
    b, _, t_y, t_x = mask.shape
    cum_duration = torch.cumsum(duration, -1)
    cum_duration_flat = cum_duration.view(b * t_x)
    path = sequence_mask(cum_duration_flat, t_y).to(mask.dtype)
    path = path.view(b, t_x, t_y)
    path = path - F.pad(path, [0, 0, 1, 0, 0, 0])[:, :-1]
    path = path.transpose(1, 2).unsqueeze(1).contiguous() * mask
    return path
PYEOF
fi

cat > "$TRAIN_DIR/hey_neo_config.yaml" <<YAML
model_name: "hey_neo"
target_phrase:
  - "hey neo"
  - "hey, neo"
  - "hey   neo"
custom_negative_phrases:
  - "hey leo"
  - "hey neo please"
  - "they know"
  - "day neo"
n_samples: 5000
n_samples_val: 500
tts_batch_size: 5
augmentation_batch_size: 8
piper_sample_generator_path: "${PSG_PATH}"
piper_voices:
  - "${VOICES_DIR}/en_US-lessac-medium.onnx"
  - "${VOICES_DIR}/en_US-ryan-medium.onnx"
  - "${VOICES_DIR}/en_GB-alan-medium.onnx"
output_dir: "${TRAIN_DIR}"
rir_paths: []
background_paths:
  - "${TRAIN_DIR}/background"
background_paths_duplication_rate:
  - 1
false_positive_validation_data_path: "${TRAIN_DIR}/validation_set_features.npy"
augmentation_rounds: 2
feature_data_files: {}
batch_n_per_class:
  adversarial_negative: 64
  positive: 64
model_type: "dnn"
layer_size: 32
steps: 10000
max_negative_weight: 1000
target_false_positives_per_hour: 0.5
target_accuracy: 0.7
target_recall: 0.3
YAML

# ---------------------------------------------------------------------------
echo "[8/9] Training 'Hey Neo' model (this takes 3-5 hours on Pi 4 CPU)..."
# ---------------------------------------------------------------------------
cd "$TRAIN_DIR"

# our generate_samples.py shim reads voice paths from this env var
export PIPER_VOICE_PATHS="${VOICES_DIR}/en_US-lessac-medium.onnx,${VOICES_DIR}/en_US-ryan-medium.onnx,${VOICES_DIR}/en_GB-alan-medium.onnx"

echo "  Step 8a: Generating TTS clips..."
"$PY" -m openwakeword.train \
    --training_config "$TRAIN_DIR/hey_neo_config.yaml" \
    --generate_clips

echo "  Step 8b: Augmenting clips with background noise..."
"$PY" -m openwakeword.train \
    --training_config "$TRAIN_DIR/hey_neo_config.yaml" \
    --augment_clips

echo "  Step 8c: Training model..."
"$PY" -m openwakeword.train \
    --training_config "$TRAIN_DIR/hey_neo_config.yaml" \
    --train_model

# Copy the trained model to the wakeword directory
cp "$TRAIN_DIR/hey_neo/hey_neo.onnx" "$DIR/hey_neo.onnx"
echo "  Model saved to: $DIR/hey_neo.onnx"

# ---------------------------------------------------------------------------
echo "[9/9] Installing systemd service..."
# ---------------------------------------------------------------------------
sudo cp "$DIR/wakeword.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable wakeword

echo ""
echo "=== Done ==="
echo ""
echo "  NEXT STEPS:"
echo "  1. sudo systemctl start wakeword"
echo "  2. sudo journalctl -u wakeword -f    # watch live"
echo ""
echo "  Say 'Hey Neo', then: 'movie time', 'next song', 'thunderstruck', etc."
echo ""
echo "  Tune detection threshold in wakeword/config.env:"
echo "    OWW_THRESHOLD=0.5  (raise to 0.6 if false positives; lower to 0.4 if missing)"
echo ""
echo "  Optional: for richer model quality, add real background audio"
echo "  (FreeSound/Audioset clips) to training/background/ then re-run with --retrain"
