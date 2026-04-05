#!/usr/bin/env python3
"""
ReSpeaker Lite I2C Control Utility

Controls the XMOS XU316 and TLV320AIC3204 on the ReSpeaker Lite
board via I2C from a Raspberry Pi.

XMOS (0x42) - Mic processing: mute, speaker enable, VNR readback
AIC3204 (0x18) - Playback volume: headphone & line-out gain

Usage:
    python3 respeaker_control.py status          # Show all status
    python3 respeaker_control.py volume 70       # Set playback volume (0-100%)
    python3 respeaker_control.py play file.wav    # Play with volume control applied
    python3 respeaker_control.py mute            # Toggle mute button status
    python3 respeaker_control.py speaker on      # Enable/disable speaker amp
    python3 respeaker_control.py vnr             # Read Voice-to-Noise Ratio
"""

import sys
import time
import subprocess
import smbus2

I2C_BUS = 1
XMOS_ADDR = 0x42
AIC3204_ADDR = 0x18

# XMOS RESID and commands
RESID_DFU = 0xF0
RESID_CFG = 0xF1
CMD_SPEAKER_EN = 0x10   # Write: 0=mute speaker, 1=enable speaker
CMD_VNR = 0x80          # Read: Voice-to-Noise Ratio (0-100)
CMD_MUTE_STATUS = 0x81  # Read: mute button status (0=not muted, 1=muted)
CMD_FW_VERSION = 0xD8   # Read: firmware version (3 bytes)

# AIC3204 registers
AIC_PAGE_REG = 0x00
# Page 0: DAC digital volume (main volume control)
AIC_LDAC_VOL = 0x41     # Left DAC digital volume  (0dB to -63.5dB + mute)
AIC_RDAC_VOL = 0x42     # Right DAC digital volume
# Page 1: Analog output driver gain (kept at fixed 0 dB)
AIC_HPL_GAIN = 0x10     # Headphone Left driver gain
AIC_HPR_GAIN = 0x11     # Headphone Right driver gain
AIC_LOL_GAIN = 0x12     # Line-out Left driver gain
AIC_LOR_GAIN = 0x13     # Line-out Right driver gain

ALSA_CARD = "ReSpeakerLite"
ALSA_SOFTVOL_CTL = "ReSpeaker PV"
ALSA_SOFTVOL_PCM = "respeaker"


class ReSpeakerControl:
    def __init__(self, bus_num=I2C_BUS):
        self.bus = smbus2.SMBus(bus_num)

    def close(self):
        self.bus.close()

    # --- XMOS commands ---

    def xmos_read(self, resid, cmd, num_bytes):
        """Read from XMOS via Command Transport Protocol."""
        self.bus.write_i2c_block_data(XMOS_ADDR, resid, [cmd, num_bytes + 1])
        time.sleep(0.01)
        data = self.bus.read_i2c_block_data(XMOS_ADDR, 0, num_bytes + 1)
        return data[1:num_bytes + 1]  # skip status byte

    def xmos_write(self, resid, cmd, values):
        """Write to XMOS via Command Transport Protocol."""
        if isinstance(values, int):
            values = [values]
        self.bus.write_i2c_block_data(XMOS_ADDR, resid, [cmd, len(values)] + values)
        time.sleep(0.01)

    def get_firmware_version(self):
        data = self.xmos_read(RESID_DFU, CMD_FW_VERSION, 3)
        return f"v{data[0]}.{data[1]}.{data[2]}"

    def get_vnr(self):
        """Get Voice-to-Noise Ratio (0-100). Higher = more voice detected."""
        data = self.xmos_read(RESID_CFG, CMD_VNR, 1)
        return data[0]

    def get_mute_status(self):
        """Get mute button status. True = muted."""
        data = self.xmos_read(RESID_CFG, CMD_MUTE_STATUS, 1)
        return bool(data[0])

    def set_speaker_enable(self, enable):
        """Enable or disable the speaker amplifier (AUDIO_PA_EN pin)."""
        self.xmos_write(RESID_CFG, CMD_SPEAKER_EN, 1 if enable else 0)

    # --- AIC3204 commands ---

    def aic_write_reg(self, reg, value):
        self.bus.write_byte_data(AIC3204_ADDR, reg, value)
        time.sleep(0.005)

    def aic_read_reg(self, reg):
        return self.bus.read_byte_data(AIC3204_ADDR, reg)

    def set_hardware_minimum(self):
        """Set AIC3204 hardware to maximum attenuation (-69.5 dB total).

        Analog output driver gain: -6 dB (Page 1, R16-R19 = 0x3A)
        DAC digital volume: -63.5 dB (Page 0, R65-R66 = 0x7F)
        """
        # Analog gain = -6 dB on all outputs
        self.aic_write_reg(AIC_PAGE_REG, 0x01)
        for reg in (AIC_HPL_GAIN, AIC_HPR_GAIN, AIC_LOL_GAIN, AIC_LOR_GAIN):
            self.aic_write_reg(reg, 0x3A)
        # DAC digital volume = -63.5 dB
        self.aic_write_reg(AIC_PAGE_REG, 0x00)
        self.aic_write_reg(AIC_LDAC_VOL, 0x7F)
        self.aic_write_reg(AIC_RDAC_VOL, 0x7F)

    def set_volume(self, percent):
        """
        Set playback volume (0-100%) using ALSA software volume only.

        ALSA softvol range: -110 dB (0%) to 0 dB (100%).
        Just use: aplay -D respeaker file.wav
        """
        percent = max(0, min(100, percent))
        self._ensure_softvol_control()
        result = subprocess.run(
            ["amixer", "-c", ALSA_CARD, "sset", ALSA_SOFTVOL_CTL, f"{percent}%"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"Error setting volume: {result.stderr.strip()}")
            return
        import re
        m = re.search(r'\[([^\]]+dB)\]', result.stdout)
        db_str = m.group(1) if m else f"{percent}%"
        print(f"Volume: {percent}% ({db_str})")

    def get_volume(self):
        """Read current volume from ALSA softvol control."""
        self._ensure_softvol_control()
        result = subprocess.run(
            ["amixer", "-c", ALSA_CARD, "sget", ALSA_SOFTVOL_CTL],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return None, "softvol not available"
        import re
        m = re.search(r'\[(\d+)%\]\s*\[([^\]]+)\]', result.stdout)
        if m:
            return int(m.group(1)), m.group(2)
        m = re.search(r'\[(\d+)%\]', result.stdout)
        if m:
            return int(m.group(1)), f"{int(m.group(1))}%"
        return None, "unknown"

    def _ensure_softvol_control(self):
        """Create the softvol ALSA control if it doesn't exist yet."""
        result = subprocess.run(
            ["amixer", "-c", ALSA_CARD, "sget", ALSA_SOFTVOL_CTL],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            # Open and immediately close the softvol device to create the control
            subprocess.run(
                ["aplay", "-D", ALSA_SOFTVOL_PCM, "-d", "1", "-f", "S32_LE",
                 "-r", "16000", "-c", "2", "-q", "/dev/zero"],
                timeout=5, capture_output=True
            )

    def play(self, wav_file, fmt="S32_LE", rate="16000", channels="2"):
        """Play a WAV file through the softvol device."""
        self._ensure_softvol_control()
        result = subprocess.run(
            ["aplay", "-D", ALSA_SOFTVOL_PCM, "-f", fmt, "-r", rate,
             "-c", channels, wav_file],
            stderr=subprocess.PIPE
        )
        if result.returncode != 0:
            stderr = result.stderr.decode().strip()
            if stderr:
                print(f"Playback error: {stderr}")

    def status(self):
        """Print full status."""
        print(f"Firmware:    {self.get_firmware_version()}")
        print(f"VNR:         {self.get_vnr()} / 100")
        print(f"Mute button: {'muted' if self.get_mute_status() else 'not muted'}")
        pct, db_str = self.get_volume()
        print(f"Volume:      {pct}% ({db_str})")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    ctrl = ReSpeakerControl()
    cmd = sys.argv[1].lower()

    try:
        if cmd == "status":
            ctrl.status()

        elif cmd == "volume":
            if len(sys.argv) < 3:
                pct, db_str = ctrl.get_volume()
                print(f"Volume: {pct}% ({db_str})")
            else:
                ctrl.set_volume(int(sys.argv[2]))

        elif cmd == "speaker":
            if len(sys.argv) < 3:
                print("Usage: speaker on|off")
            else:
                enable = sys.argv[2].lower() in ("on", "1", "true")
                ctrl.set_speaker_enable(enable)
                print(f"Speaker: {'enabled' if enable else 'disabled'}")

        elif cmd == "play":
            if len(sys.argv) < 3:
                print("Usage: play <file.wav> [format] [rate] [channels]")
                print("  e.g.: play /tmp/test.wav S32_LE 16000 2")
            else:
                wav = sys.argv[2]
                fmt = sys.argv[3] if len(sys.argv) > 3 else "S32_LE"
                rate = sys.argv[4] if len(sys.argv) > 4 else "16000"
                ch = sys.argv[5] if len(sys.argv) > 5 else "2"
                ctrl.play(wav, fmt, rate, ch)

        elif cmd == "vnr":
            print(f"VNR: {ctrl.get_vnr()} / 100")

        elif cmd == "mute":
            print(f"Mute button: {'muted' if ctrl.get_mute_status() else 'not muted'}")

        elif cmd == "firmware":
            print(f"Firmware: {ctrl.get_firmware_version()}")

        else:
            print(f"Unknown command: {cmd}")
            print(__doc__)
            sys.exit(1)
    finally:
        ctrl.close()


if __name__ == "__main__":
    main()
