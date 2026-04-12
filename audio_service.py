#!/usr/bin/env python3
"""
qBc_Audio MQTT Service

Provides audio recording, playback, and wake word detection via MQTT.

MQTT topics:
    Subscribe:
        robot/audio/cmd      General commands (JSON):
                             {"command": "record"}
                             {"command": "stop_recording"}
                             {"command": "stop_playing"}
                             {"command": "clear_trigger"}
        robot/audio/play     Play audio file:
                             {"file": "name.wav"}

    Publish:
        robot/audio/wake_word       {"model": "...", "score": 0.xx}
        robot/audio/state           (RETAIN) {"status":"online", "listening":bool, ...}
        robot/audio/recording_ready {"file": "/path/to/voice_xxx.wav"}
        robot/system/heartbeat/audio  keepalive (1 Hz)

Usage:
    python3 audio_service.py [--mqtt-broker localhost] [--mqtt-port 1883] [--threshold 0.5]
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import paho.mqtt.client as mqtt
from openwakeword.model import Model as WakeWordModel

from respeaker_control import find_devices, ensure_asoundrc, ReSpeakerControl, ALSA_CARD, ALSA_SOFTVOL_PCM

logger = logging.getLogger("qBc_Audio")

BASE_DIR = Path(__file__).parent
RECORDINGS_DIR = BASE_DIR / "resources" / "recordings"
PLAYBACK_DIR = BASE_DIR / "resources" / "playback"
SOUNDS_DIR = BASE_DIR / "resources" / "sounds"
WAKE_WORD_DIR = BASE_DIR / "resources" / "wake_word_model"
WAKEWORD_ACK_SOUND = str(SOUNDS_DIR / "wakeword-ack.wav")

# Audio capture settings (ReSpeaker Lite native format)
SAMPLE_RATE = 16000
CHANNELS = 2
SAMPLE_WIDTH = 4  # S32_LE = 4 bytes per sample
ARECORD_FMT = "S32_LE"

# Wake word settings
WAKE_CHUNK_SAMPLES = 1280  # 80 ms at 16 kHz — openwakeword frame size
DEFAULT_WAKE_THRESHOLD = 0.5
TRIGGER_DURATION = 2.0  # seconds

# MQTT topics
TOPIC_CMD = "robot/audio/cmd"
TOPIC_PLAY = "robot/audio/play"
TOPIC_WAKE_WORD = "robot/audio/wake_word"
TOPIC_SPEECH_TEXT = "robot/audio/speech_text"
TOPIC_STATE = "robot/audio/state"
TOPIC_HEARTBEAT = "robot/system/heartbeat/audio"
TOPIC_RECORDING_READY = "robot/audio/recording_ready"
TOPIC_CURRENT_STATE = "robot/audio/current_state"
TOPIC_ERROR_INFO = "robot/audio/error_info"
TOPIC_SETTINGS_AUDIO = "robot/settings/audio"

# Voice recording (auto-triggered by wake word)
VOICE_REC_TIMEOUT = 10.0          # max seconds
VOICE_SILENCE_THRESHOLD = 500     # int16 RMS level for silence
VOICE_SILENCE_DURATION = 1.5      # seconds of silence to auto-stop
VOICE_MIN_DURATION = 0.5          # minimum seconds before silence-stop

# TTS voice effects (sox) — applied only to voice=True playback
# Set to None to disable
VOICE_EFFECTS = [
    "highpass", "600",
    "pitch", "800",              # higher pitch (cents) — cute robot
    "tempo", "1.25",               # slightly faster
    "tremolo", "60", "50",      # subtle warble
    "echo", "0.8", "0.8", "4", "0.8",  # short echo
    "overdrive", "3",                # prevent clipping
]


class AudioService:
    def __init__(self, broker="localhost", port=1883, wake_threshold=DEFAULT_WAKE_THRESHOLD):
        self.broker = broker
        self.port = port
        self.wake_threshold = wake_threshold

        # Discover ALSA devices and ensure .asoundrc is up to date
        devs = find_devices()
        if devs is None:
            raise RuntimeError(
                f"ALSA card '{ALSA_CARD}' not found. Is the overlay loaded?"
            )
        if ensure_asoundrc(devs):
            logger.info("Regenerated ~/.asoundrc for hw:%s,%s",
                        devs["card_name"], devs["playback_dev"])
        self.capture_pcm = devs["capture_pcm"]
        self.playback_pcm = ALSA_SOFTVOL_PCM
        logger.info("Capture: %s, Playback: -D %s", self.capture_pcm, self.playback_pcm)

        # ReSpeaker hardware control (for volume)
        try:
            self._respeaker = ReSpeakerControl()
        except Exception:
            self._respeaker = None
            logger.warning("ReSpeaker I2C control unavailable — volume control disabled")

        # Volume settings (updated via MQTT)
        self._global_volume = 100

        # State
        self._listening = False
        self._recording = False
        self._playing = False
        self._triggered = False
        self._trigger_time = 0.0
        self._lock = threading.Lock()

        # Capture
        self._capture_proc = None
        self._capture_thread = None
        self._running = False

        # Recording buffer
        self._rec_frames = []

        # Voice recording state
        self._voice_recording = False
        self._voice_rec_start = 0.0
        self._voice_silence_start = 0.0

        # Playback
        self._play_proc = None

        # Wake word model
        self._wake_model = None
        self._load_wake_model()

        # MQTT client
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="qbc_audio",
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.will_set(
            TOPIC_STATE,
            json.dumps({"status": "offline"}),
            qos=1, retain=True,
        )
        self.connected = False
        self._error_info = "E_OK"

    # ------------------------------------------------------------------
    # Wake word model
    # ------------------------------------------------------------------

    def _load_wake_model(self):
        models = list(WAKE_WORD_DIR.glob("*.tflite")) + list(WAKE_WORD_DIR.glob("*.onnx"))
        if not models:
            logger.warning("No wake word models in %s — detection disabled", WAKE_WORD_DIR)
            return
        model_paths = [str(m) for m in models]
        logger.info("Loading wake word models: %s", [m.name for m in models])
        self._wake_model = WakeWordModel(
            wakeword_models=model_paths,
            inference_framework="onnx",
        )

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _get_state(self):
        with self._lock:
            # Auto-expire trigger
            if self._triggered and (time.monotonic() - self._trigger_time) > TRIGGER_DURATION:
                self._triggered = False
            return {
                "listening": self._listening,
                "recording": self._recording,
                "playing": self._playing,
                "triggered": self._triggered,
                "voice_recording": self._voice_recording,
            }

    def _derive_current_state(self):
        """Derive human-readable current state from internal flags."""
        with self._lock:
            if self._playing:
                return "playing"
            if self._voice_recording:
                return "voice_recording"
            if self._recording:
                return "recording"
            if self._triggered:
                return "wake_word_triggered"
            if self._listening:
                return "listening"
            return "idle"

    def _set_error(self, error):
        """Set and publish error info."""
        self._error_info = error
        if self.connected:
            self._client.publish(TOPIC_ERROR_INFO, error, qos=1, retain=True)

    # ------------------------------------------------------------------
    # Current state / error helpers
    # ------------------------------------------------------------------

    def _derive_current_state(self):
        """Derive human-readable current state from internal flags."""
        with self._lock:
            if self._playing:
                return "playing"
            if self._voice_recording:
                return "voice_recording"
            if self._recording:
                return "recording"
            if self._triggered:
                return "wake_word_triggered"
            if self._listening:
                return "listening"
            return "idle"

    def _set_error(self, error):
        """Set and publish error info."""
        self._error_info = error
        if self.connected:
            self._client.publish(TOPIC_ERROR_INFO, error, qos=1, retain=True)

    # ------------------------------------------------------------------
    # MQTT publish helpers
    # ------------------------------------------------------------------

    def _publish_state(self):
        """Publish current state to robot/audio/state (retained)."""
        state = {"status": "online", **self._get_state()}
        self._client.publish(TOPIC_STATE, json.dumps(state), qos=1, retain=True)
        self._client.publish(TOPIC_CURRENT_STATE, self._derive_current_state(), qos=1, retain=True)
        self._client.publish(TOPIC_ERROR_INFO, self._error_info, qos=1, retain=True)
        self._client.publish(TOPIC_CURRENT_STATE, self._derive_current_state(), qos=1, retain=True)
        self._client.publish(TOPIC_ERROR_INFO, self._error_info, qos=1, retain=True)

    # ------------------------------------------------------------------
    # MQTT callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties):
        if reason_code.is_failure:
            logger.error("MQTT connection failed: %s", reason_code)
            return
        self.connected = True
        logger.info("Connected to MQTT broker %s:%d", self.broker, self.port)
        client.subscribe([(TOPIC_CMD, 1), (TOPIC_PLAY, 1), (TOPIC_SETTINGS_AUDIO, 1)])
        self._publish_state()

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        self.connected = False
        if reason_code.is_failure:
            logger.warning("Disconnected from MQTT broker: %s", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Invalid JSON on %s", msg.topic)
            return

        if msg.topic == TOPIC_SETTINGS_AUDIO:
            self._global_volume = max(0, min(100, int(data.get("global_volume", self._global_volume))))
            logger.info("Global volume updated: %d%%", self._global_volume)
            return

        if msg.topic == TOPIC_PLAY:
            resp = self.handle_play(
                data.get("file"), volume=data.get("volume"), voice=data.get("voice", False),
            )
        elif msg.topic == TOPIC_CMD:
            cmd = data.get("command", "")
            if cmd == "record":
                resp = self.handle_record()
            elif cmd == "stop_recording":
                resp = self.handle_stop_recording()
            elif cmd == "stop_playing":
                resp = self.handle_stop_playing()
            elif cmd == "clear_trigger":
                resp = self.handle_clear_trigger()
            elif cmd == "get_state":
                self._publish_state()
                return
            else:
                logger.warning("Unknown audio command: %s", cmd)
                return
        else:
            return

        if resp and resp.get("status") == "error":
            logger.warning("Audio command error: %s", resp.get("message"))

    # ------------------------------------------------------------------
    # Audio capture (background thread)
    # ------------------------------------------------------------------

    def _start_capture(self):
        if self._capture_proc is not None:
            return
        self._capture_proc = subprocess.Popen(
            [
                "arecord", "-D", self.capture_pcm,
                "-f", ARECORD_FMT, "-r", str(SAMPLE_RATE),
                "-c", str(CHANNELS), "-t", "raw", "-q",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def _stop_capture(self):
        if self._capture_proc is not None:
            self._capture_proc.terminate()
            try:
                self._capture_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._capture_proc.kill()
            self._capture_proc = None

    def _capture_loop(self):
        chunk_bytes = WAKE_CHUNK_SAMPLES * CHANNELS * SAMPLE_WIDTH

        while self._running:
            with self._lock:
                need_capture = self._listening or self._recording

            if not need_capture:
                if self._capture_proc is not None:
                    self._stop_capture()
                time.sleep(0.05)
                continue

            if self._capture_proc is None:
                self._start_capture()

            data = self._capture_proc.stdout.read(chunk_bytes)
            if not data or len(data) < chunk_bytes:
                self._stop_capture()
                time.sleep(0.1)
                continue

            with self._lock:
                is_recording = self._recording
                is_listening = self._listening
                is_voice = self._voice_recording

            # Accumulate raw frames while recording
            if is_recording:
                self._rec_frames.append(data)

            # Voice recording: check silence and timeout
            if is_voice:
                samples_v = np.frombuffer(data, dtype=np.int32)
                mono_v = (samples_v[0::2] >> 16).astype(np.int16)
                rms = np.sqrt(np.mean(mono_v.astype(np.float32) ** 2))

                now = time.monotonic()
                elapsed = now - self._voice_rec_start

                stop_voice = False
                if rms < VOICE_SILENCE_THRESHOLD:
                    if self._voice_silence_start == 0.0:
                        self._voice_silence_start = now
                    elif (now - self._voice_silence_start >= VOICE_SILENCE_DURATION
                          and elapsed >= VOICE_MIN_DURATION):
                        stop_voice = True
                else:
                    self._voice_silence_start = 0.0

                if elapsed >= VOICE_REC_TIMEOUT:
                    stop_voice = True

                if stop_voice:
                    self._finish_voice_recording()

            # Feed wake word model while listening
            if is_listening and self._wake_model is not None:
                # S32_LE stereo → int16 mono (left channel)
                samples = np.frombuffer(data, dtype=np.int32)
                mono = (samples[0::2] >> 16).astype(np.int16)

                predictions = self._wake_model.predict(mono)

                with self._lock:
                    # Auto-expire trigger
                    if self._triggered and (time.monotonic() - self._trigger_time) > TRIGGER_DURATION:
                        self._triggered = False
                    already_triggered = self._triggered

                if not already_triggered:
                    for name, score in predictions.items():
                        if score >= self.wake_threshold:
                            self._on_trigger(name, score)
                            break

        self._stop_capture()

    def _on_trigger(self, model_name, score):
        with self._lock:
            if self._triggered:
                return
            self._triggered = True
            self._trigger_time = time.monotonic()

            # Auto-start voice recording
            if not self._recording:
                self._listening = False
                self._recording = True
                self._voice_recording = True
                self._rec_frames = []
                self._voice_rec_start = time.monotonic()
                self._voice_silence_start = 0.0

        logger.info("Wake word: %s (score=%.3f) — recording voice", model_name, score)

        # Play acknowledgment sound immediately (non-blocking)
        if os.path.isfile(WAKEWORD_ACK_SOUND):
            threading.Thread(
                target=self._play_ack_sound, daemon=True,
            ).start()

        self._client.publish(
            TOPIC_WAKE_WORD,
            json.dumps({"model": model_name, "score": round(float(score), 3)}),
            qos=1,
        )
        self._publish_state()

    def _play_ack_sound(self):
        """Play the wake-word acknowledgment sound in the background.

        Uses a short subprocess so it doesn't interfere with the main
        playback state (_playing flag) or voice recording.
        """
        try:
            subprocess.run(
                ["aplay", "-D", self.playback_pcm, WAKEWORD_ACK_SOUND],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except Exception as e:
            logger.debug("Ack sound failed: %s", e)

    def _finish_voice_recording(self):
        """Stop voice recording, save as 16-bit mono WAV, publish recording_ready."""
        with self._lock:
            if not self._voice_recording:
                return
            self._recording = False
            self._voice_recording = False
            self._voice_silence_start = 0.0
            self._triggered = False
            self._listening = self._wake_model is not None

        # Reset wake word model state for clean next detection
        if self._wake_model is not None:
            self._wake_model.reset()

        frames = self._rec_frames
        self._rec_frames = []

        if not frames:
            logger.warning("Voice recording empty")
            self._publish_state()
            return

        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = RECORDINGS_DIR / f"voice_{timestamp}.wav"

        # Convert S32_LE stereo → 16-bit mono (left channel)
        raw = b"".join(frames)
        samples = np.frombuffer(raw, dtype=np.int32)
        mono = (samples[0::2] >> 16).astype(np.int16)

        with wave.open(str(filepath), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(mono.tobytes())

        duration = len(mono) / SAMPLE_RATE
        logger.info("Voice recording saved: %s (%.1fs)", filepath, duration)
        self._client.publish(
            TOPIC_RECORDING_READY,
            json.dumps({"file": str(filepath)}),
            qos=1,
        )
        self._publish_state()

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def handle_record(self):
        with self._lock:
            if self._recording:
                return {"status": "error", "message": "Already recording"}
            self._listening = False
            self._recording = True
            self._rec_frames = []

        self._publish_state()
        logger.info("Recording started")
        return {"status": "ok", "message": "Recording started"}

    def handle_stop_recording(self):
        with self._lock:
            if not self._recording:
                return {"status": "error", "message": "Not recording"}
            self._recording = False
            self._voice_recording = False
            self._voice_silence_start = 0.0
            self._listening = self._wake_model is not None

        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = RECORDINGS_DIR / f"rec_{timestamp}.wav"

        frames = self._rec_frames
        self._rec_frames = []
        raw = b"".join(frames)

        with wave.open(str(filepath), "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(raw)

        self._publish_state()
        logger.info("Recording saved: %s", filepath)
        return {"status": "ok", "file": str(filepath)}

    def handle_play(self, file_path=None, volume=None, voice=False):
        with self._lock:
            if self._playing:
                return {"status": "error", "message": "Already playing"}

        if file_path is None:
            return {"status": "error", "message": "No file specified"}

        if volume is not None and self._respeaker is not None:
            # Apply global volume scaling
            effective = round(int(volume) * self._global_volume / 100)
            try:
                self._respeaker.set_volume(effective)
                logger.info("Volume set to %d%% (requested=%s, global=%d%%)",
                            effective, volume, self._global_volume)
            except Exception as e:
                logger.warning("Failed to set volume: %s", e)

        # Resolve relative paths against PLAYBACK_DIR and SOUNDS_DIR
        if not os.path.isabs(file_path):
            resolved_playback = (PLAYBACK_DIR / file_path).resolve()
            resolved_sounds = (SOUNDS_DIR / file_path).resolve()

            if resolved_sounds.is_file() and str(resolved_sounds).startswith(str(SOUNDS_DIR.resolve())):
                file_path = str(resolved_sounds)
            else:
                if not str(resolved_playback).startswith(str(PLAYBACK_DIR.resolve())):
                    return {"status": "error", "message": "Invalid file path"}
                file_path = str(resolved_playback)

        if not os.path.isfile(file_path):
            return {"status": "error", "message": f"File not found: {file_path}"}

        with self._lock:
            self._playing = True

        thread = threading.Thread(
            target=self._playback_worker, args=(file_path, voice), daemon=True,
        )
        thread.start()

        self._publish_state()
        logger.info("Playback: %s (voice=%s)", file_path, voice)
        return {"status": "ok", "file": file_path}

    def _apply_voice_effects(self, file_path):
        """Apply robot voice effects via sox. Returns path to processed file."""
        if not VOICE_EFFECTS:
            return file_path
        effected = file_path + ".fx.wav"
        try:
            result = subprocess.run(
                ["sox", file_path, effected] + VOICE_EFFECTS,
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0 and os.path.isfile(effected):
                return effected
            logger.warning("Sox effects failed: %s", result.stderr)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.warning("Sox unavailable, playing without effects: %s", e)
        return file_path

    def _playback_worker(self, file_path, voice=False):
        effected_path = None
        playback_ok = True
        try:
            play_path = file_path
            if voice:
                play_path = self._apply_voice_effects(file_path)
                if play_path != file_path:
                    effected_path = play_path

            self._play_proc = subprocess.Popen(
                ["aplay", "-D", self.playback_pcm, play_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self._play_proc.wait()
            if self._play_proc.returncode != 0:
                err = self._play_proc.stderr.read().decode().strip()
                if err:
                    logger.error("Playback error: %s", err)
                    self._set_error("playback: " + err[:80])
                    playback_ok = False
        except Exception as e:
            logger.error("Playback exception: %s", e)
            self._set_error("playback: " + str(e)[:80])
            playback_ok = False
        finally:
            self._play_proc = None
            # Clean up temp effects file
            if effected_path:
                try:
                    os.remove(effected_path)
                except OSError:
                    pass
            with self._lock:
                self._playing = False
            if playback_ok:
                self._set_error("E_OK")
            self._publish_state()

    def handle_stop_playing(self):
        if self._play_proc is not None:
            self._play_proc.terminate()
            logger.info("Playback stopped")
            return {"status": "ok", "message": "Playback stopped"}
        return {"status": "error", "message": "Not playing"}

    def handle_clear_trigger(self):
        with self._lock:
            self._triggered = False
        if self._wake_model is not None:
            self._wake_model.reset()
        self._publish_state()
        logger.info("Trigger cleared")
        return {"status": "ok", "message": "Trigger cleared"}

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self):
        self._running = True

        # Start wake word listening if a model is loaded
        self._listening = self._wake_model is not None
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()

        # Connect MQTT
        self._client.connect(self.broker, self.port)
        self._client.loop_start()

        logger.info("qBc_Audio service on MQTT %s:%d", self.broker, self.port)
        if self._wake_model:
            logger.info("Wake word detection active")
        else:
            logger.info("Wake word detection disabled (no models)")

        # Block until signal, publish heartbeat every second
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())

        while not stop.is_set():
            self._client.publish(TOPIC_HEARTBEAT, b"1", qos=0)
            stop.wait(1.0)

        logger.info("Shutting down...")
        self._running = False
        self._client.publish(TOPIC_STATE, json.dumps({"status": "offline"}), qos=1, retain=True)
        self._client.loop_stop()
        self._client.disconnect()
        if self._play_proc:
            self._play_proc.terminate()
        self._stop_capture()


def main():
    parser = argparse.ArgumentParser(description="qBc_Audio MQTT Service")
    parser.add_argument("--mqtt-broker", default="localhost", help="MQTT broker address")
    parser.add_argument("--mqtt-port", type=int, default=1883, help="MQTT broker port")
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_WAKE_THRESHOLD,
        help="Wake word detection threshold (0.0-1.0)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    service = AudioService(
        broker=args.mqtt_broker,
        port=args.mqtt_port,
        wake_threshold=args.threshold,
    )
    service.run()


if __name__ == "__main__":
    main()
