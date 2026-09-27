"""Microphone capture and speaker playback (sounddevice / PortAudio).

Run `python -m jarvis.voice.audio` for a mic check: lists devices, records 3 s, plays it back.
"""

from __future__ import annotations

import logging
import threading

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000  # what faster-whisper expects for numpy input


class AudioError(Exception):
    """Microphone or speaker problem. The message is safe to show the user."""


class Recorder:
    """Open-ended mono recording: start(), then stop() returns float32 samples at 16 kHz."""

    def __init__(self, max_seconds: float, sample_rate: int = SAMPLE_RATE) -> None:
        self._sample_rate = sample_rate
        self._max_frames = int(max_seconds * sample_rate)
        self._chunks: list[np.ndarray] = []
        self._frames = 0
        self._lock = threading.Lock()
        self._stream: sd.InputStream | None = None

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self._stream is not None:
            return
        self._chunks, self._frames = [], 0
        try:
            self._stream = sd.InputStream(
                samplerate=self._sample_rate, channels=1, dtype="float32", callback=self._on_audio
            )
            self._stream.start()
        except sd.PortAudioError as e:
            self._stream = None
            logger.error("Could not open microphone: %s", e)
            raise AudioError("Could not open the microphone. Check that it is connected and allowed.") from None

    def stop(self) -> np.ndarray:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()
            stream.close()
        with self._lock:
            if not self._chunks:
                return np.zeros(0, dtype=np.float32)
            return np.concatenate(self._chunks)[: self._max_frames, 0]

    def _on_audio(self, indata: np.ndarray, frames: int, time: object, status: sd.CallbackFlags) -> None:
        # Runs on the PortAudio thread: copy and return quickly.
        if status:
            logger.warning("Audio input status: %s", status)
        with self._lock:
            if self._frames < self._max_frames:
                self._chunks.append(indata.copy())
                self._frames += frames


def play(samples: np.ndarray, sample_rate: int) -> None:
    """Play samples and block until done."""
    try:
        sd.play(samples, sample_rate)
        sd.wait()
    except sd.PortAudioError as e:
        logger.error("Playback failed: %s", e)
        raise AudioError("Could not play audio. Check the speaker/headphone device.") from None


def pcm16_to_float(data: bytes) -> np.ndarray:
    """Raw 16-bit little-endian mono PCM -> float32 in [-1, 1]. A trailing odd byte is dropped."""
    return np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0


def play_pcm16(data: bytes, sample_rate: int) -> None:
    """Play raw 16-bit little-endian mono PCM at its original volume."""
    play(pcm16_to_float(data), sample_rate)


def list_devices() -> str:
    return str(sd.query_devices())


def _mic_check(seconds: float = 3.0) -> None:
    print(list_devices())
    print(f"\nDefault (input, output): {sd.default.device}")
    input(f"\nPress Enter, then speak for {seconds:g} seconds...")
    recorder = Recorder(max_seconds=seconds)
    recorder.start()
    sd.sleep(int(seconds * 1000))
    samples = recorder.stop()
    peak = float(np.abs(samples).max()) if samples.size else 0.0
    print(f"Recorded {samples.size / SAMPLE_RATE:.1f}s, peak level {peak:.2f} (speech is usually > 0.05).")
    print("Playing it back...")
    play(samples, SAMPLE_RATE)
    print("Done.")


if __name__ == "__main__":
    _mic_check()
