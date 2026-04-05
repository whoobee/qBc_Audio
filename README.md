# qBc_Audio

Audio subsystem for the qB_Companion robot. Provides I2S microphone capture and speaker playback using a **Seeed Studio ReSpeaker Lite** connected to a **Raspberry Pi 5** via GPIO.

## Hardware

- **Board:** Seeed Studio ReSpeaker Lite
- **XMOS XU316** — AI mic-processing chip (I2C address `0x42`)
- **TLV320AIC3204** — Audio codec (I2C address `0x18`)
- **Firmware:** I2S mode v1.0.9
- **Interface:** I2S (audio) + I2C (control), no USB required after setup

## Wiring

| ReSpeaker Lite | Pi 5 GPIO | Physical Pin |
|----------------|-----------|--------------|
| BCLK           | GPIO18    | Pin 12       |
| LRCLK          | GPIO19    | Pin 35       |
| DOUT           | GPIO20    | Pin 38       |
| DIN            | GPIO21    | Pin 40       |
| SDA (D4)       | GPIO2     | Pin 3        |
| SCL (D5)       | GPIO3     | Pin 5        |

> **Note:** DOUT on the ReSpeaker connects to GPIO20 (the Pi's DIN), and DIN on the ReSpeaker connects to GPIO21 (the Pi's DOUT).

## File Structure

```
qBc_Audio/
├── respeaker_control.py          # CLI utility for volume, playback, and XMOS/AIC3204 control
├── scripts/
│   ├── install.sh                # Automated setup script (firmware flash, overlay, ALSA config)
│   └── respeaker-lite.dts        # Device tree overlay source
└── README.md
```

## Installation

Connect the ReSpeaker Lite via both GPIO (I2S/I2C wiring above) and USB (for one-time firmware flash), then run:

```bash
sudo ./scripts/install.sh
sudo reboot
```

After reboot, the USB cable can be disconnected. The install script:

1. Removes the incompatible `seeed-voicecard` driver
2. Flashes I2S firmware v1.0.9 onto the XMOS chip (via USB)
3. Compiles and installs the device tree overlay
4. Configures `/boot/firmware/config.txt` (I2S + I2C + overlay)
5. Installs ALSA softvol configuration (`~/.asoundrc`) and `respeaker_control.py`
6. Detects and configures PipeWire or PulseAudio (if present)
7. Rebuilds kernel module dependencies

## Usage

### Recording (capture)

```bash
arecord -D hw:ReSpeakerLite,0 -f S32_LE -r 16000 -c 2 -d 5 recording.wav
```

### Playback (with software volume control)

```bash
aplay -D respeaker -f S32_LE -r 16000 -c 2 recording.wav
```

### Volume control

```bash
# Using the control utility
python3 respeaker_control.py volume 50

# Using amixer directly
amixer -c ReSpeakerLite sset 'ReSpeaker PV' 50%
```

### Control utility commands

| Command              | Description                              |
|----------------------|------------------------------------------|
| `status`             | Show firmware version, VNR, mute, volume |
| `volume [0-100]`     | Get or set playback volume               |
| `play <file.wav>`    | Play a WAV file through softvol          |
| `speaker on\|off`    | Enable/disable speaker amplifier         |
| `vnr`                | Read Voice-to-Noise Ratio (0–100)        |
| `mute`               | Read mute button status                  |
| `firmware`           | Read XMOS firmware version               |

## ALSA Devices

| PCM Name         | Type     | Description                          |
|------------------|----------|--------------------------------------|
| `respeaker`      | softvol  | Playback with software volume        |
| `respeaker_cap`  | hw       | Capture (raw hardware)               |
| `hw:ReSpeakerLite,0` | hw   | Capture (raw)                        |
| `hw:ReSpeakerLite,1` | hw   | Playback (raw, bypasses softvol)     |

## Volume Architecture

Volume is handled entirely in software via ALSA's `softvol` plugin, which wraps the raw hardware playback device (`hw:ReSpeakerLite,1`). The softvol range is **-33 dB to +10 dB** with 256 steps of resolution. The XMOS chip resets the AIC3204 codec registers on each new audio stream, so hardware volume registers are unreliable — softvol avoids this problem.

## Compatibility

- **Raspberry Pi OS** (Debian Trixie, kernel 6.12+) — bare ALSA, works directly
- **Ubuntu Desktop** — PipeWire detected and configured automatically
- **Ubuntu Server with PulseAudio** — PulseAudio ALSA sink configured automatically

## Device Tree Overlay

The overlay (`respeaker-lite.dts`) creates a `simple-audio-card` with two DAI links using dummy codecs:

- **Link 0** (`spdif-dir`): Capture — XMOS mic data → Pi
- **Link 1** (`spdif-dit`): Playback — Pi → XMOS → speaker

The Pi operates as I2S clock consumer (the XMOS chip is the bus master).
