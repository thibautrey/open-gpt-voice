"""OpenAI-compatible text-to-speech adapter.

Targets local speech servers exposing ``POST /v1/audio/speech`` (Kokoro-FastAPI,
Voxtral TTS wrappers, Supertonic gateways). The session expects mono 24 kHz
PCM16LE, so WAV or raw PCM responses are decoded and resampled here.
"""

from __future__ import annotations

import io
import json
import wave
from collections.abc import Callable, Mapping
from threading import Event

import numpy as np
from loguru import logger

from ..ports.speaker import Speaker

_TARGET_RATE = 24_000


class OpenAICompatibleSpeaker:
    """Synthesize speech through an OpenAI-compatible speech endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        voice_map: Mapping[str, str] | None = None,
        response_format: str = "wav",
        timeout_s: float = 60.0,
        transport: Callable[[str, bytes, Mapping[str, str]], bytes] | None = None,
    ) -> None:
        self.url = base_url.rstrip("/") + "/audio/speech"
        self.model = model
        self.api_key = api_key
        self.voice_map = dict(voice_map or {})
        self.response_format = response_format
        self.timeout_s = timeout_s
        self.transport = transport
        self.log = logger.bind(component="openai-tts")

    def synthesize(self, text: str, voice: str, cancel_event: Event | None = None) -> bytes:
        """Return mono 24 kHz PCM16LE for one utterance."""
        if not text.strip():
            raise ValueError("text must not be empty")
        if cancel_event is not None and cancel_event.is_set():
            return b""
        payload = {
            "model": self.model,
            "input": text,
            "voice": self.voice_map.get(voice, voice),
            "response_format": self.response_format,
        }
        body = json.dumps(payload).encode("utf-8")
        if self.transport is not None:
            raw = self.transport(self.url, body, self._headers())
        else:
            raw = self._post(body)
        if cancel_event is not None and cancel_event.is_set():
            return b""
        return _to_session_pcm(raw, self.response_format)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _post(self, body: bytes) -> bytes:
        import httpx

        with httpx.Client(timeout=self.timeout_s) as client:
            response = client.post(self.url, content=body, headers=self._headers())
            response.raise_for_status()
            return response.content


def _to_session_pcm(raw: bytes, response_format: str) -> bytes:
    """Decode a WAV or raw PCM response into 24 kHz mono PCM16LE."""
    if not raw:
        return b""
    if response_format == "pcm" or raw[:4] not in {b"RIFF", b"OggS"}:
        samples = np.frombuffer(raw, dtype=np.int16)
        return samples.astype(np.int16).tobytes()
    with wave.open(io.BytesIO(raw), "rb") as wav:
        channels = wav.getnchannels()
        sample_rate = wav.getframerate()
        width = wav.getsampwidth()
        frames = wav.readframes(wav.getnframes())
    if width != 2:
        raise RuntimeError(f"Unsupported WAV sample width: {width * 8} bits")
    samples = np.frombuffer(frames, dtype=np.int16)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return _resample(samples, sample_rate, _TARGET_RATE)


def _resample(samples: np.ndarray, source_rate: int, target_rate: int) -> bytes:
    if source_rate == target_rate or samples.size == 0:
        return samples.astype(np.int16).tobytes()
    target_size = max(1, int(round(samples.size * target_rate / source_rate)))
    resampled = np.interp(
        np.linspace(0, samples.size - 1, target_size, dtype=np.float64),
        np.arange(samples.size, dtype=np.float64),
        samples.astype(np.float32),
    )
    return np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()
