"""OpenAI-compatible transcription adapter.

Serves Parakeet, Voxtral, faster-whisper or any server exposing
``POST /v1/audio/transcriptions``. The session hands over one completed turn as
24 kHz mono PCM16LE; this adapter wraps it in a WAV container and returns the
transcript with session-relative timing.
"""

from __future__ import annotations

import io
import wave
from collections.abc import Callable, Mapping

import httpx
from loguru import logger

from ..ports.asr import Transcript

_SAMPLE_RATE = 24_000
_BYTES_PER_SAMPLE = 2


class OpenAICompatibleASR:
    """Transcribe a turn through an OpenAI-compatible audio endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        language: str | None = None,
        timeout_s: float = 60.0,
        transport: Callable[[str, bytes, Mapping[str, str], Mapping[str, str]], bytes] | None = None,
    ) -> None:
        self.url = base_url.rstrip("/") + "/audio/transcriptions"
        self.model = model
        self.api_key = api_key
        self.language = language
        self.timeout_s = timeout_s
        self.transport = transport
        self.log = logger.bind(component="openai-asr")

    def transcribe(self, pcm16le: bytes, start_ms: int) -> Transcript:
        """Return the transcript for one 24 kHz mono PCM16LE turn."""
        if not pcm16le:
            return Transcript("", start_ms, start_ms)
        duration_ms = len(pcm16le) // (_SAMPLE_RATE * _BYTES_PER_SAMPLE) * 1000
        wav_bytes = _wav_container(pcm16le)
        data = {"model": self.model, "response_format": "json"}
        if self.language:
            data["language"] = self.language
        if self.transport is not None:
            raw = self.transport(self.url, wav_bytes, self._headers(), data)
        else:
            raw = self._post(wav_bytes, data)
        text = _transcript_text(raw)
        return Transcript(text, start_ms, start_ms + duration_ms)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _post(self, wav_bytes: bytes, data: dict[str, str]) -> bytes:
        headers = self._headers()
        files = {"file": ("turn.wav", wav_bytes, "audio/wav")}
        with httpx.Client(timeout=self.timeout_s) as client:
            response = client.post(self.url, headers=headers, data=data, files=files)
            response.raise_for_status()
            return response.content


def _wav_container(pcm16le: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(_SAMPLE_RATE)
        wav.writeframes(pcm16le)
    return buffer.getvalue()


def _transcript_text(raw: bytes) -> str:
    import json

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("Transcription endpoint returned invalid JSON") from exc
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        raise RuntimeError("Transcription endpoint returned no text field")
    return text.strip()
