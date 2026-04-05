#!/bin/bash
#
# ReSpeaker Lite I2S Setup Script for Raspberry Pi 5
# ---------------------------------------------------
# This script configures a Seeed Studio ReSpeaker Lite (XMOS XU316)
# to work over I2S with a Raspberry Pi 5 running Debian Trixie.
#
# What it does:
#   1. Removes any existing seeed-voicecard driver (wrong driver for this board)
#   2. Flashes the I2S firmware onto the XMOS XU316 (requires USB connection)
#   3. Compiles and installs a device tree overlay (dummy codec, raw I2S capture)
#   4. Configures /boot/firmware/config.txt
#   5. Installs ALSA softvol config (~/.asoundrc) and respeaker_control.py
#   6. Detects PipeWire/PulseAudio and configures them for softvol
#   7. Rebuilds module dependencies
#
# Prerequisites:
#   - Raspberry Pi 5 running Debian Trixie (kernel 6.12+)
#   - ReSpeaker Lite connected via GPIO (I2S + I2C pins)
#   - ReSpeaker Lite also connected via USB (for firmware flash only)
#   - Run as root: sudo ./install.sh
#
# After running this script, reboot the Pi. You can then disconnect USB.
# The microphone capture will work over I2S via GPIO.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIRMWARE_URL="https://github.com/respeaker/ReSpeaker_Lite/raw/master/xmos_firmwares/respeaker_lite_i2s_dfu_firmware_v1.0.9.bin"
FIRMWARE_FILE="/tmp/respeaker_lite_i2s_v1.0.9.bin"
OVERLAY_NAME="respeaker-lite"
CONFIG_FILE="/boot/firmware/config.txt"
OVERLAYS_DIR="/boot/firmware/overlays"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ------------------------------------------------------------------
# Check prerequisites
# ------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (use sudo)"
fi

for cmd in dtc dfu-util i2cdetect wget amixer; do
    if ! command -v "$cmd" &>/dev/null; then
        info "Installing missing tool: $cmd"
        case "$cmd" in
            dtc)       apt-get install -y device-tree-compiler ;;
            dfu-util)  apt-get install -y dfu-util ;;
            i2cdetect) apt-get install -y i2c-tools ;;
            wget)      apt-get install -y wget ;;
            amixer)    apt-get install -y alsa-utils ;;
        esac
    fi
done

# ------------------------------------------------------------------
# Step 1: Remove seeed-voicecard driver if present
# ------------------------------------------------------------------
info "Step 1: Removing seeed-voicecard driver (if present)..."

if systemctl is-enabled seeed-voicecard.service &>/dev/null; then
    systemctl disable --now seeed-voicecard.service
    info "Disabled seeed-voicecard service"
fi

if dkms status 2>/dev/null | grep -q "seeed-voicecard"; then
    VER=$(dkms status 2>/dev/null | grep seeed-voicecard | head -1 | sed 's/.*\///' | cut -d',' -f1)
    dkms remove "seeed-voicecard/$VER" --all 2>/dev/null || true
    info "Removed seeed-voicecard DKMS module"
fi

# Unload modules if loaded
for mod in snd_soc_ac108 snd_soc_seeed_voicecard snd_soc_wm8960; do
    rmmod "$mod" 2>/dev/null || true
done

# Blacklist to prevent auto-loading
cat > /etc/modprobe.d/blacklist-seeed-voicecard.conf << 'BLACKLIST'
blacklist snd_soc_seeed_voicecard
blacklist snd_soc_ac108
BLACKLIST
info "Blacklisted seeed-voicecard modules"

# Clean up source
rm -rf /usr/src/seeed-voicecard-* 2>/dev/null || true

# ------------------------------------------------------------------
# Step 2: Flash I2S firmware onto XMOS XU316
# ------------------------------------------------------------------
info "Step 2: Checking XMOS firmware..."

if ! lsusb | grep -q "2886:0019"; then
    error "ReSpeaker Lite not found on USB. Please connect it via USB for firmware flashing."
fi

# Check current firmware version
CURRENT_FW=$(dfu-util -l 2>&1 | grep "ver=" | head -1 | sed 's/.*ver=\([0-9]*\).*/\1/')
if [[ "$CURRENT_FW" == "0109" ]]; then
    info "I2S firmware v1.0.9 already installed, skipping flash"
else
    info "Current firmware version: $CURRENT_FW — flashing I2S firmware v1.0.9..."
    wget -q "$FIRMWARE_URL" -O "$FIRMWARE_FILE"
    dfu-util -R -e -a 1 -D "$FIRMWARE_FILE" 2>&1 || true
    info "Firmware flashed. Waiting for device to restart..."
    sleep 5

    # Verify
    NEW_FW=$(dfu-util -l 2>&1 | grep "ver=" | head -1 | sed 's/.*ver=\([0-9]*\).*/\1/')
    if [[ "$NEW_FW" == "0109" ]]; then
        info "I2S firmware v1.0.9 confirmed"
    else
        warn "Firmware version reported: $NEW_FW (expected 0109). May need a power cycle."
    fi
    rm -f "$FIRMWARE_FILE"
fi

# ------------------------------------------------------------------
# Step 3: Compile and install device tree overlay
# ------------------------------------------------------------------
info "Step 3: Building device tree overlay..."

DTS_FILE="${SCRIPT_DIR}/respeaker-lite.dts"
if [[ ! -f "$DTS_FILE" ]]; then
    error "Device tree source not found: $DTS_FILE"
fi

dtc -@ -I dts -O dtb -o "/tmp/${OVERLAY_NAME}.dtbo" "$DTS_FILE" 2>&1
cp "/tmp/${OVERLAY_NAME}.dtbo" "${OVERLAYS_DIR}/${OVERLAY_NAME}.dtbo"
rm -f "/tmp/${OVERLAY_NAME}.dtbo"
info "Overlay installed to ${OVERLAYS_DIR}/${OVERLAY_NAME}.dtbo"

# ------------------------------------------------------------------
# Step 4: Configure /boot/firmware/config.txt
# ------------------------------------------------------------------
info "Step 4: Configuring ${CONFIG_FILE}..."

# Ensure i2c_arm is enabled
if grep -q "^dtparam=i2c_arm=off" "$CONFIG_FILE"; then
    sed -i 's/^dtparam=i2c_arm=off/dtparam=i2c_arm=on/' "$CONFIG_FILE"
    info "Enabled i2c_arm"
elif ! grep -q "^dtparam=i2c_arm=on" "$CONFIG_FILE"; then
    # Add it in the [all] section
    sed -i '/^\[all\]/a dtparam=i2c_arm=on' "$CONFIG_FILE"
    info "Added i2c_arm=on"
fi

# Ensure i2s is enabled in [all] section
if ! grep -A 100 '^\[all\]' "$CONFIG_FILE" | grep -q "^dtparam=i2s=on"; then
    sed -i '/^\[all\]/a dtparam=i2s=on' "$CONFIG_FILE"
    info "Added i2s=on to [all] section"
fi

# Remove conflicting overlays from [all] section
for overlay in "i2s-mmap" "rpi-i2s-generic" "seeed-2mic-voicecard" "googlevoicehat-soundcard"; do
    sed -i "/^dtoverlay=${overlay}/d" "$CONFIG_FILE"
done

# Comment out any old seeed-voicecard references
sed -i 's/^dtoverlay=seeed-/#dtoverlay=seeed-/' "$CONFIG_FILE"

# Add our overlay if not present
if ! grep -q "^dtoverlay=${OVERLAY_NAME}" "$CONFIG_FILE"; then
    echo "dtoverlay=${OVERLAY_NAME}" >> "$CONFIG_FILE"
    info "Added dtoverlay=${OVERLAY_NAME}"
else
    info "dtoverlay=${OVERLAY_NAME} already present"
fi

# ------------------------------------------------------------------
# Step 5: Install ALSA softvol configuration
# ------------------------------------------------------------------
info "Step 5: Installing ALSA softvol configuration..."

# Determine target user's home (the user who ran sudo)
TARGET_USER="${SUDO_USER:-$(whoami)}"
TARGET_HOME=$(eval echo "~${TARGET_USER}")
ASCFG="${TARGET_HOME}/.asoundrc"

# Install .asoundrc (back up existing if different)
ASRCFILE="${SCRIPT_DIR}/asoundrc"
if [[ ! -f "$ASRCFILE" ]]; then
    # Generate the asoundrc inline
    cat > "$ASRCFILE" << 'ASOUNDEOF'
# ReSpeaker Lite: software volume control wrapping the hardware device
# Playback: aplay -D respeaker -f S32_LE -r 16000 -c 2 file.wav
# Capture:  arecord -D respeaker_cap -f S32_LE -r 16000 -c 2 file.wav

pcm.respeaker {
    type softvol
    slave.pcm "hw:ReSpeakerLite,1"
    control {
        name "ReSpeaker PV"
        card ReSpeakerLite
    }
    min_dB -33.0
    max_dB 10.0
    resolution 256
}

pcm.respeaker_cap {
    type hw
    card ReSpeakerLite
    device 0
}
ASOUNDEOF
fi

if [[ -f "$ASCFG" ]] && ! diff -q "$ASRCFILE" "$ASCFG" &>/dev/null; then
    cp "$ASCFG" "${ASCFG}.bak"
    warn "Backed up existing ${ASCFG} to ${ASCFG}.bak"
fi
cp "$ASRCFILE" "$ASCFG"
chown "${TARGET_USER}:${TARGET_USER}" "$ASCFG"
info "Installed ${ASCFG} (softvol device: respeaker)"

# Install respeaker_control.py
CONTROL_PY="${SCRIPT_DIR}/respeaker_control.py"
if [[ -f "$CONTROL_PY" ]]; then
    cp "$CONTROL_PY" /usr/local/bin/respeaker_control.py
    chmod +x /usr/local/bin/respeaker_control.py
    info "Installed respeaker_control.py to /usr/local/bin/"
fi

# Ensure smbus2 is available
if ! python3 -c "import smbus2" 2>/dev/null; then
    info "Installing smbus2 Python package..."
    pip3 install smbus2 --break-system-packages 2>/dev/null || apt-get install -y python3-smbus2 2>/dev/null || true
fi

# ------------------------------------------------------------------
# Step 5b: Handle PipeWire / PulseAudio (Ubuntu Desktop)
# ------------------------------------------------------------------
# On systems with PipeWire or PulseAudio, the audio server grabs the
# sound card and may bypass .asoundrc softvol. We configure it to use
# our ALSA softvol device instead of the raw hardware.

HAS_PIPEWIRE=false
HAS_PULSEAUDIO=false

if systemctl --user -M "${TARGET_USER}@" is-active pipewire.service &>/dev/null 2>&1 || \
   pgrep -u "${TARGET_USER}" pipewire &>/dev/null 2>&1 || \
   command -v pipewire &>/dev/null; then
    HAS_PIPEWIRE=true
fi

if ! $HAS_PIPEWIRE; then
    if systemctl --user -M "${TARGET_USER}@" is-active pulseaudio.service &>/dev/null 2>&1 || \
       pgrep -u "${TARGET_USER}" pulseaudio &>/dev/null 2>&1 || \
       command -v pulseaudio &>/dev/null; then
        HAS_PULSEAUDIO=true
    fi
fi

if $HAS_PIPEWIRE; then
    info "PipeWire detected — configuring ALSA bridge..."

    # PipeWire ALSA config: tell PipeWire to use our softvol device
    PW_ALSA_DIR="${TARGET_HOME}/.config/pipewire/pipewire.conf.d"
    mkdir -p "$PW_ALSA_DIR"
    cat > "${PW_ALSA_DIR}/respeaker-lite.conf" << 'PWEOF'
# Route ReSpeaker Lite playback through ALSA softvol
context.modules = [
    {   name = libpipewire-module-loopback
        args = {
            node.description = "ReSpeaker Playback"
            capture.props = {
                media.class    = "Audio/Sink"
                node.name      = "respeaker_playback"
                node.description = "ReSpeaker Lite Speaker"
                audio.format   = S32LE
                audio.rate     = 16000
                audio.channels = 2
            }
            playback.props = {
                node.name      = "respeaker_alsa_out"
                media.class    = "Audio/Sink"
                api.alsa.pcm.device = "respeaker"
                api.alsa.open.ucm = false
            }
        }
    }
]
PWEOF
    chown -R "${TARGET_USER}:${TARGET_USER}" "${TARGET_HOME}/.config/pipewire"
    info "Installed PipeWire config: ${PW_ALSA_DIR}/respeaker-lite.conf"
    warn "You may need to restart PipeWire: systemctl --user restart pipewire"

elif $HAS_PULSEAUDIO; then
    info "PulseAudio detected — configuring ALSA sink..."

    PA_DIR="${TARGET_HOME}/.config/pulse"
    mkdir -p "$PA_DIR"

    # Add ALSA sink using our softvol device
    PA_DEFAULT="${PA_DIR}/default.pa"
    if [[ ! -f "$PA_DEFAULT" ]]; then
        # Include system default first
        echo ".include /etc/pulse/default.pa" > "$PA_DEFAULT"
    fi

    if ! grep -q "respeaker" "$PA_DEFAULT" 2>/dev/null; then
        cat >> "$PA_DEFAULT" << 'PAEOF'

# ReSpeaker Lite softvol playback
load-module module-alsa-sink device=respeaker sink_name=respeaker_sink sink_properties=device.description="ReSpeaker-Lite"
set-default-sink respeaker_sink
PAEOF
        info "Added PulseAudio ALSA sink for softvol device"
    fi

    chown -R "${TARGET_USER}:${TARGET_USER}" "$PA_DIR"
    warn "You may need to restart PulseAudio: systemctl --user restart pulseaudio"

else
    info "No PipeWire or PulseAudio detected — bare ALSA, softvol works directly"
fi

# ------------------------------------------------------------------
# Step 7: Rebuild module dependencies
# ------------------------------------------------------------------
depmod -a
info "Module dependencies rebuilt"

# ------------------------------------------------------------------
# Done
# ------------------------------------------------------------------
echo ""
echo "============================================="
echo " ReSpeaker Lite I2S setup complete!"
echo "============================================="
echo ""
echo " Please reboot to apply all changes:"
echo "   sudo reboot"
echo ""
echo " After reboot you can disconnect the USB cable."
echo " The mic array will work over I2S via GPIO."
echo ""
echo " Test recording (capture):"
echo "   arecord -D hw:2,0 -f S32_LE -r 16000 -c 2 -d 5 test.wav"
echo ""
echo " Test playback (with software volume control):"
echo "   aplay -D respeaker -f S32_LE -r 16000 -c 2 test.wav"
echo ""
echo " Set volume (0-100%):"
echo "   respeaker_control.py volume 50"
echo ""
echo " Or use amixer directly:"
echo "   amixer -c ReSpeakerLite sset 'ReSpeaker PV' 50%"
echo ""
echo " Show status:"
echo "   respeaker_control.py status"
echo "=============================================="
