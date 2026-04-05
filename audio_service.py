#!/usr/bin/env python3
"""
qBc_Audio WebSocket Service

Provides audio recording, playback, and wake word detection via WebSocket.

WebSocket API (JSON):
    {"command": "record"}                        Start microphone recording
    {"command": "stop_recording"}                Stop recording, save to file
    {"command": "play", "file": "name.wav"}      Play audio file (relative to resources/playback or absolute)
    {"command": "stop_playing"}                  Stop current playback
    {"command": "clear_trigger"}                 Clear wake word trigger state
    {"command": "get_state"}                     Get current service state

Broadcasts to all clients:
    {"event": "triggered", "model": "...", "score": 0.xx}
    {"event": "state", "listening": bool, "recording": bool, "playing": bool, "triggered": bool}
    {"event": "playback_finished"}

Usage:
    python3 audio_service.py [--port 8766] [--host 0.0.0.0] [--threshold 0.5]
"""

import asyncio
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
import websockets
from openwakeword.model import Model as WakeWordModel

from respeaker_control import find_devices, ALSA_CARD, ALSA_SOFTVOL_PCM

logger = logging.getLogger("qBc_Audio")

BASE_DIR = Path(__file__).parent
RECORDINGS_DIR = BASE_DIR / "resources" / "recordings"
PLAYBACK_DIR = BASE_DIR / "resources" / "playback"
WAKE_WORD_DIR = BASE_DIR / "resources" / "wake_word_model"

# Audio capture settings (ReSpeaker Lite native format)
SAMPLE_RATE = 16000
CHANNELS = 2
SAMPLE_WIDTH = 4  # S32_LE = 4 bytes per sample
ARECORD_FMT = "S32_LE"

# Wake word settings
WAKE_CHUNK_SAMPLES = 1280  # 80 ms at 16 kHz — openwakeword frame size
DEFAULT_WAKE_THRESHOLD = 0.5
TRIGGER_DURATION = 2.0  # seconds


class AudioService:
    def __init__(self, host="0.0.0.0", port=8766, wake_threshold=DEFAULT_WAKE_THRESHOLD):
        self.host = host
        self.port = port
        self.wake_threshold = wake_threshold

        # Discover ALSA devices
        devs = find_devices()
        if devs is None:
            raise RuntimeError(
                f"ALSA card '{ALSA_CARD}' not found. Is the overlay loaded?"
            )
        self.capture_pcm = devs["capture_pcm"]
        self.playback_pcm = ALSA_SOFTVOL_PCM
        logger.info("Capture: %s, Playback: -D %s", self.capture_pcm, self.playback_pcm)

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

        # Playback
        self._play_proc = None

        # Wake word model
        self._wake_model = None
        self._load_wake_model()

        # WebSocket clients
        self._clients = set()
        self._loop = None

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
            }

    # ------------------------------------------------------------------
    # WebSocket broadcast helpers
    # ------------------------------------------------------------------

    def _broadcast(self, message):
        if self._loop is None or not self._clients:
            return
        data = json.dumps(message)
        asyncio.run_coroutine_threadsafe(self._async_broadcast(data), self._loop)

    async def _async_broadcast(self, data):
        if self._clients:
            await asyncio.gather(
                *[c.send(data) for c in self._clients],
                return_exceptions=True,
            )

    def _broadcast_state(self):
        self._broadcast({"event": "state", **self._get_state()})

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

            # Accumulate raw frames while recording
            if is_recording:
                self._rec_frames.append(data)

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

        logger.info("Wake word: %s (score=%.3f)", model_name, score)
        self._broadcast({
            "event": "triggered",
            "model": model_name,
            "score": round(score, 3),
        })
        self._broadcast_state()

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

        self._broadcast_state()
        logger.info("Recording started")
        return {"status": "ok", "message": "Recording started"}

    def handle_stop_recording(self):
        with self._lock:
            if not self._recording:
                return {"status": "error", "message": "Not recording"}
            self._recording = False
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

        self._broadcast_state()
        logger.info("Recording saved: %s", filepath)
        return {"status": "ok", "file": str(filepath)}

    def handle_play(self, file_path=None):
        with self._lock:
            if self._playing:
                return {"status": "error", "message": "Already playing"}

        if file_path is None:
            return {"status": "error", "message": "No file specified"}

        # Resolve relative paths against the default playback directory
        if not os.path.isabs(file_path):
            resolved = (PLAYBACK_DIR / file_path).resolve()
            if not str(resolved).startswith(str(PLAYBACK_DIR.resolve())):
                return {"status": "error", "message": "Invalid file path"}
            file_path = str(resolved)

        if not os.path.isfile(file_path):
            return {"status": "error", "message": f"File not found: {file_path}"}

        with self._lock:
            self._playing = True

        thread = threading.Thread(target=self._playback_worker, args=(file_path,), daemon=True)
        thread.start()

        self._broadcast_state()
        logger.info("Playback: %s", file_path)
        return {"status": "ok", "file": file_path}

    def _playback_worker(self, file_path):
        try:
            self._play_proc = subprocess.Popen(
                ["aplay", "-D", self.playback_pcm, file_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self._play_proc.wait()
            if self._play_proc.returncode != 0:
                err = self._play_proc.stderr.read().decode().strip()
                if err:
                    logger.error("Playback error: %s", err)
        except Exception as e:
            logger.error("Playback exception: %s", e)
        finally:
            self._play_proc = None
            with self._lock:
                self._playing = False
            self._broadcast({"event": "playback_finished"})
            self._broadcast_state()

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
        self._broadcast_state()
        logger.info("Trigger cleared")
        return {"status": "ok", "message": "Trigger cleared"}

    # ------------------------------------------------------------------
    # WebSocket server
    # ------------------------------------------------------------------

    async def _ws_handler(self, websocket):
        self._clients.add(websocket)
        remote = websocket.remote_address
        logger.info("Client connected: %s", remote)
        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send(json.dumps(
                        {"status": "error", "message": "Invalid JSON"}
                    ))
                    continue

                cmd = msg.get("command", "")

                if cmd == "record":
                    resp = self.handle_record()
                elif cmd == "stop_recording":
                    resp = self.handle_stop_recording()
                elif cmd == "play":
                    resp = self.handle_play(msg.get("file"))
                elif cmd == "stop_playing":
                    resp = self.handle_stop_playing()
                elif cmd == "clear_trigger":
                    resp = self.handle_clear_trigger()
                elif cmd == "get_state":
                    resp = {"status": "ok", **self._get_state()}
                else:
                    resp = {"status": "error", "message": f"Unknown command: {cmd}"}

                await websocket.send(json.dumps(resp))
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(websocket)
            logger.info("Client disconnected: %s", remote)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self):
        self._loop = asyncio.get_running_loop()
        self._running = True

        # Start wake word listening if a model is loaded
        self._listening = self._wake_model is not None
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()

        # Graceful shutdown on SIGTERM / SIGINT
        stop = self._loop.create_future()
        for sig in (signal.SIGTERM, signal.SIGINT):
            self._loop.add_signal_handler(sig, stop.set_result, None)

        async with websockets.serve(self._ws_handler, self.host, self.port):
            logger.info("qBc_Audio service on ws://%s:%d", self.host, self.port)
            if self._wake_model:
                logger.info("Wake word detection active")
            else:
                logger.info("Wake word detection disabled (no models)")
            await stop

        logger.info("Shutting down...")
        self._running = False
        if self._play_proc:
            self._play_proc.terminate()
        self._stop_capture()


def main():
    parser = argparse.ArgumentParser(description="qBc_Audio WebSocket Service")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=8766, help="WebSocket port")
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
        host=args.host,
        port=args.port,
        wake_threshold=args.threshold,
    )
    asyncio.run(service.run())


if __name__ == "__main__":
    main()
