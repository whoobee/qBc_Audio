#!/bin/bash
# Install Python dependencies for qBc_Audio
# openwakeword requires --no-deps because tflite-runtime has no wheel for Python 3.13 aarch64
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pip install -r "${SCRIPT_DIR}/requirements.txt"
pip install openwakeword --no-deps
