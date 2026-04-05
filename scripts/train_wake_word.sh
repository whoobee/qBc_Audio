#!/bin/bash
#
# Train a Custom Wake Word Model for openwakeword
# ------------------------------------------------
# This script automates the full training pipeline:
#   1. Sets up the environment (venv, dependencies, repos)
#   2. Downloads required data (negative features, RIRs, noise)
#   3. Generates synthetic TTS clips of your wake phrase
#   4. Augments clips with noise/reverb
#   5. Trains the model
#   6. Outputs a .onnx file ready for use
#
# Requirements:
#   - Linux x86_64 machine (desktop, server, or Colab)
#   - Python 3.10 (3.12+ not supported by training deps)
#   - python3.10-dev package installed (for building C-extensions)
#   - ~10 GB disk space
#   - GPU recommended but CPU works (slower)
#   - Internet access for downloads
#
# Usage:
#   ./train_wake_word.sh                           # Interactive: prompts for wake phrase
#   ./train_wake_word.sh "hey glados"              # Direct: specify wake phrase
#   ./train_wake_word.sh "hey glados" --steps 50000 --samples 50000  # Custom params
#
# The trained model will be in: output/<model_name>/<model_name>.onnx
# Copy it to qBc_Audio/resources/wake_word_model/ on your Pi.

set -euo pipefail

# ------------------------------------------------------------------
# Configuration (override via env vars or CLI args)
# ------------------------------------------------------------------
WAKE_PHRASE="${1:-}"
MODEL_NAME=""
N_SAMPLES="${N_SAMPLES:-10000}"
N_SAMPLES_VAL="${N_SAMPLES_VAL:-2000}"
TRAIN_STEPS="${TRAIN_STEPS:-25000}"
MODEL_TYPE="${MODEL_TYPE:-dnn}"
LAYER_SIZE="${LAYER_SIZE:-64}"
TARGET_FP_PER_HOUR="${TARGET_FP_PER_HOUR:-0.2}"

# Parse optional CLI args
shift 2>/dev/null || true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --steps)      TRAIN_STEPS="$2";  shift 2 ;;
        --samples)    N_SAMPLES="$2";    shift 2 ;;
        --name)       MODEL_NAME="$2";   shift 2 ;;
        --type)       MODEL_TYPE="$2";   shift 2 ;;
        --layer-size) LAYER_SIZE="$2";  shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
step()  { echo -e "\n${CYAN}========== $* ==========${NC}"; }

# ------------------------------------------------------------------
# Platform checks
# ------------------------------------------------------------------
ARCH=$(uname -m)
if [[ "$ARCH" == "aarch64" || "$ARCH" == "armv7l" ]]; then
    warn "You're on $ARCH (likely a Raspberry Pi)."
    warn "Training requires x86_64 Linux with ~10GB RAM and ideally a GPU."
    warn "Copy this script to a desktop/server or use Google Colab."
    read -rp "Continue anyway? (y/N) " yn
    [[ "$yn" =~ ^[Yy] ]] || exit 0
fi

# ------------------------------------------------------------------
# Interactive wake phrase input
# ------------------------------------------------------------------
if [[ -z "$WAKE_PHRASE" ]]; then
    echo ""
    echo "Enter the wake word or phrase to train (e.g., 'hey glados', 'ok robot'):"
    read -rp "> " WAKE_PHRASE
fi

[[ -z "$WAKE_PHRASE" ]] && error "No wake phrase specified"

# Generate model name from phrase if not set
if [[ -z "$MODEL_NAME" ]]; then
    MODEL_NAME=$(echo "$WAKE_PHRASE" | tr '[:upper:]' '[:lower:]' | tr -cs '[:alnum:]' '_' | sed 's/_$//')
fi

info "Wake phrase:  \"$WAKE_PHRASE\""
info "Model name:   $MODEL_NAME"
info "Samples:      $N_SAMPLES (+ $N_SAMPLES_VAL validation)"
info "Train steps:  $TRAIN_STEPS"
info "Architecture: $MODEL_TYPE (layer size $LAYER_SIZE)"
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="${SCRIPT_DIR}/train_workdir"
OUTPUT_DIR="${WORK_DIR}/output"
VENV_DIR="${WORK_DIR}/.venv"

mkdir -p "$WORK_DIR" "$OUTPUT_DIR"
cd "$WORK_DIR"

# ------------------------------------------------------------------
# Step 1: Python virtual environment + dependencies
# ------------------------------------------------------------------
step "Step 1/6: Setting up Python environment"

# Prefer python3.11 (piper-phonemize requires <=3.11), fall back to python3.10, then python3
PYTHON_BIN=""
for candidate in python3.11 python3.10 python3; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON_BIN="$candidate"
        break
    fi
done
[[ -z "$PYTHON_BIN" ]] && error "No Python 3 found"

if [[ ! -d "$VENV_DIR" ]]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    info "Created venv at $VENV_DIR (using $PYTHON_BIN)"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# Check Python version
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [[ "$PY_MAJOR" -ne 3 ]] || [[ "$PY_MINOR" -lt 10 ]] || [[ "$PY_MINOR" -gt 12 ]]; then
    warn "Python $PY_VER detected. Training is tested with Python 3.10-3.12."
    warn "Some dependencies may not install correctly."
fi

# [FIX] Pinned setuptools to <70 to prevent pkg_resources ModuleNotFoundError
pip install --upgrade pip wheel "setuptools<70" -q

# Core training dependencies
info "Installing PyTorch..."
if python3 -c "import torch" 2>/dev/null; then
    info "PyTorch already installed"
else
    # Try CUDA first, fall back to CPU
    pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121 -q 2>/dev/null \
        || pip install torch torchaudio -q
fi

info "Installing training dependencies..."
# [FIX] Added onnx, onnx-tf, and tensorflow to allow phase 3 to export successfully
pip install -q \
    numpy scipy pyyaml tqdm \
    mutagen==1.47.0 \
    torchinfo==1.8.0 \
    torchmetrics==1.2.0 \
    speechbrain==0.5.14 \
    audiomentations==0.33.0 \
    torch-audiomentations==0.11.0 \
    acoustics==0.2.6 \
    pronouncing==0.2.0 \
    datasets==2.14.6 \
    deep-phonemizer==0.0.19 \
    onnxruntime \
    piper-phonemize webrtcvad \
    onnx onnx-tf tensorflow

info "Dependencies installed"

# ------------------------------------------------------------------
# Step 2: Clone repos + download models
# ------------------------------------------------------------------
step "Step 2/6: Setting up openwakeword + Piper TTS"

# openwakeword repo (for train.py)
if [[ ! -d "openwakeword" ]]; then
    git clone https://github.com/dscripka/openwakeword.git
    pip install -e ./openwakeword -q
    info "Cloned openwakeword"
else
    info "openwakeword repo already present"
fi

# [FIX] Manually download base ONNX models to bypass Git LFS issues
MODEL_DIR="openwakeword/openwakeword/resources/models"
mkdir -p "$MODEL_DIR"
if [[ ! -f "$MODEL_DIR/melspectrogram.onnx" ]]; then
    info "Downloading base melspectrogram model..."
    wget -q --show-progress -O "$MODEL_DIR/melspectrogram.onnx" \
        "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/melspectrogram.onnx"
fi
if [[ ! -f "$MODEL_DIR/embedding_model.onnx" ]]; then
    info "Downloading base embedding model..."
    wget -q --show-progress -O "$MODEL_DIR/embedding_model.onnx" \
        "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/embedding_model.onnx"
fi

# Piper sample generator
if [[ ! -d "piper-sample-generator" ]]; then
    git clone https://github.com/rhasspy/piper-sample-generator.git
    info "Cloned piper-sample-generator"
else
    info "piper-sample-generator already present"
fi

# Piper TTS model
PIPER_MODEL="piper-sample-generator/models/en_US-libritts_r-medium.pt"
if [[ ! -f "$PIPER_MODEL" ]]; then
    mkdir -p piper-sample-generator/models
    info "Downloading Piper TTS model (~1.5 GB)..."
    wget -q --show-progress -O "$PIPER_MODEL" \
        "https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt"
else
    info "Piper TTS model already present"
fi

# ------------------------------------------------------------------
# Step 3: Download training data
# ------------------------------------------------------------------
step "Step 3/6: Downloading training data"

# Pre-computed negative features from HuggingFace (~2GB)
FEATURES_FILE="openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
if [[ ! -f "$FEATURES_FILE" ]]; then
    info "Downloading negative feature data (~2 GB)..."
    pip install -q huggingface_hub
    python3 -c "
from huggingface_hub import hf_hub_download
hf_hub_download(
    repo_id='davidscripka/openwakeword_features',
    filename='openwakeword_features_ACAV100M_2000_hrs_16bit.npy',
    local_dir='.',
    repo_type='dataset',
)
print('Downloaded negative features')
"
else
    info "Negative features already present"
fi

# Validation features
VAL_FILE="validation_set_features.npy"
if [[ ! -f "$VAL_FILE" ]]; then
    info "Downloading validation features..."
    python3 -c "
from huggingface_hub import hf_hub_download
hf_hub_download(
    repo_id='davidscripka/openwakeword_features',
    filename='validation_set_features.npy',
    local_dir='.',
    repo_type='dataset',
)
print('Downloaded validation features')
"
else
    info "Validation features already present"
fi

# MIT Room Impulse Responses
if [[ ! -d "mit_rirs" ]]; then
    info "Downloading MIT Room Impulse Responses..."
    mkdir -p mit_rirs
    wget -q --show-progress -O mit_rirs.zip \
        "https://mcdermottlab.mit.edu/Reverb/IRMAudio/Audio.zip" 2>/dev/null \
    && unzip -q -o mit_rirs.zip -d mit_rirs && rm -f mit_rirs.zip \
    || warn "MIT RIR download failed — augmentation will have less reverb variety"
    
    info "Cleaning and formatting MIT RIR files..."
    # [FIX] Delete macOS metadata folder
    rm -rf mit_rirs/__MACOSX
    # [FIX] Flatten directory (moves audio out of subfolders to root of mit_rirs)
    find mit_rirs -type f -name "*.wav" -exec mv {} mit_rirs/ \;
    find mit_rirs -mindepth 1 -type d -exec rm -rf {} + 2>/dev/null || true
    
    # [FIX] Resample all MIT files to 16000 Hz to prevent ValueError crashes
    python3 -c "
import os, torchaudio
p='mit_rirs'
for f in os.listdir(p):
    if f.endswith('.wav'):
        path = os.path.join(p, f)
        w, sr = torchaudio.load(path)
        if sr != 16000:
            torchaudio.save(path, torchaudio.functional.resample(w, sr, 16000), 16000)
"
else
    info "MIT RIRs already present"
fi

# Background noise (Free Music Archive subset + AudioSet if available)
if [[ ! -d "background_noise" ]]; then
    info "Downloading background noise samples..."
    mkdir -p background_noise
    # Use FMA small subset (~7.2 GB) - skip if too large, use a smaller approach
    # For a lighter setup, we'll generate simple noise
    python3 -c "
import numpy as np, os, wave
os.makedirs('background_noise', exist_ok=True)
# Generate diverse noise profiles as fallback
for i, noise_type in enumerate(['white', 'pink', 'brown', 'babble']):
    sr = 16000
    dur = 300  # 5 min each
    n = sr * dur
    if noise_type == 'white':
        audio = np.random.randn(n).astype(np.float32) * 0.1
    elif noise_type == 'pink':
        white = np.random.randn(n)
        b = [0.049922035, -0.095993537, 0.050612699, -0.004709510]
        a = [1, -2.494956002, 2.017265875, -0.522189400]
        from scipy.signal import lfilter
        audio = lfilter(b, a, white).astype(np.float32) * 0.1
    elif noise_type == 'brown':
        audio = np.cumsum(np.random.randn(n) * 0.01).astype(np.float32)
        audio = audio / max(abs(audio.max()), abs(audio.min())) * 0.1
    elif noise_type == 'babble':
        audio = sum(np.random.randn(n) for _ in range(8)).astype(np.float32)
        audio = audio / max(abs(audio.max()), abs(audio.min())) * 0.15
    
    path = f'background_noise/{noise_type}_noise.wav'
    with wave.open(path, 'w') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes((audio * 32767).astype(np.int16).tobytes())
print('Generated background noise samples')
"
else
    info "Background noise already present"
fi

info "Training data ready"

# ------------------------------------------------------------------
# Step 4: Generate YAML config
# ------------------------------------------------------------------
step "Step 4/6: Generating training configuration"

CONFIG_FILE="${WORK_DIR}/${MODEL_NAME}_config.yaml"
cat > "$CONFIG_FILE" << YAMLEOF
# Auto-generated training config for: ${WAKE_PHRASE}

target_phrase:
  - "${WAKE_PHRASE}"

model_name: "${MODEL_NAME}"
output_dir: "${OUTPUT_DIR}"

# Clip generation
n_samples: ${N_SAMPLES}
n_samples_val: ${N_SAMPLES_VAL}
piper_sample_generator_path: "${WORK_DIR}/piper-sample-generator"
tts_batch_size: 50

# Augmentation
augmentation_rounds: 1
augmentation_batch_size: 16
rir_paths:
  - "${WORK_DIR}/mit_rirs"
background_paths:
  - "${WORK_DIR}/background_noise"
# [FIX] Added missing duplication rate required by newer openwakeword versions
background_paths_duplication_rate: [1]

# Training
steps: ${TRAIN_STEPS}
model_type: "${MODEL_TYPE}"
layer_size: ${LAYER_SIZE}
max_negative_weight: 1500
target_false_positives_per_hour: ${TARGET_FP_PER_HOUR}
batch_n_per_class:
  ACAV100M_sample: 1024
  adversarial_negative: 50
  positive: 50

# Data paths
feature_data_files:
  ACAV100M_sample: "${WORK_DIR}/${FEATURES_FILE}"

false_positive_validation_data_path: "${WORK_DIR}/${VAL_FILE}"

# Negative phrase examples (reduce false positives on similar phrases)
custom_negative_phrases: []
YAMLEOF

info "Config written to: $CONFIG_FILE"

# ------------------------------------------------------------------
# Step 5: Run training pipeline
# ------------------------------------------------------------------
step "Step 5/6: Running training pipeline"

TRAIN_SCRIPT="${WORK_DIR}/openwakeword/openwakeword/train.py"

if [[ ! -f "$TRAIN_SCRIPT" ]]; then
    error "train.py not found at $TRAIN_SCRIPT"
fi

info "Phase 1/3: Generating synthetic clips (this may take a while)..."
python3 "$TRAIN_SCRIPT" --training_config "$CONFIG_FILE" --generate_clips

info "Phase 2/3: Augmenting clips with noise/reverb..."
python3 "$TRAIN_SCRIPT" --training_config "$CONFIG_FILE" --augment_clips

info "Phase 3/3: Training model..."
python3 "$TRAIN_SCRIPT" --training_config "$CONFIG_FILE" --train_model

# ------------------------------------------------------------------
# Step 6: Collect output
# ------------------------------------------------------------------
step "Step 6/6: Collecting trained model"

ONNX_FILE=$(find "$OUTPUT_DIR" -name "*.onnx" -not -name "melspectrogram*" -not -name "embedding*" -not -name "silero*" | head -1)

if [[ -z "$ONNX_FILE" || ! -f "$ONNX_FILE" ]]; then
    error "No .onnx model found in $OUTPUT_DIR — training may have failed"
fi

FINAL_DIR="${WORK_DIR}/trained_models"
mkdir -p "$FINAL_DIR"
cp "$ONNX_FILE" "$FINAL_DIR/${MODEL_NAME}.onnx"

echo ""
echo "============================================="
echo -e " ${GREEN}Training complete!${NC}"
echo "============================================="
echo ""
echo " Model: $FINAL_DIR/${MODEL_NAME}.onnx"
echo " Size:  $(du -h "$FINAL_DIR/${MODEL_NAME}.onnx" | cut -f1)"
echo ""
echo " To use on your Pi, copy the model:"
echo "   scp $FINAL_DIR/${MODEL_NAME}.onnx pi@<your-pi>:qB_Companion/qBc_Audio/resources/wake_word_model/"
echo ""
echo " Then restart audio_service.py — it will auto-detect the new model."
echo "============================================="