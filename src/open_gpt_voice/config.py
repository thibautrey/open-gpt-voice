"""Runtime configuration for the OpenGPT-Voice server."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Mapping


class ConfigurationError(ValueError):
    """Raised when the server configuration cannot be used safely."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated settings loaded from OPENGPT_VOICE_* environment variables."""

    host: str = "127.0.0.1"
    port: int = 8000
    bearer_token: str | None = None
    silero_model_path: str | None = None
    whisper_model_path: str | None = None
    gliner_model_path: str | None = None
    gliner_device: str = "cpu"
    router_provider: str = "gliner"
    laya_model_path: str | None = None
    laya_onnx_path: str | None = None
    laya_subfolder: str = "multilingual"
    lm_studio_base_url: str = "http://127.0.0.1:1234/v1"
    lm_studio_model: str = "local-model"
    lm_studio_reasoning_effort: str = "none"
    llm_api_key: str | None = None
    asr_provider: str = "faster_whisper"
    asr_base_url: str = "http://127.0.0.1:8000/v1"
    asr_model: str = "whisper-1"
    asr_api_key: str | None = None
    asr_language: str | None = None
    tts_provider: str = "supertonic"
    tts_base_url: str = "http://127.0.0.1:8880/v1"
    tts_model: str = "kokoro"
    tts_api_key: str | None = None
    tts_response_format: str = "wav"
    turn_provider: str = "vad"
    turn_url: str = "http://127.0.0.1:8090/turn"
    turn_threshold: float = 0.5
    delegation_mode: str = "client"
    responses_base_url: str = "http://127.0.0.1:1234/v1"
    responses_model: str = "local-model"
    responses_api_key: str | None = None
    webrtc_ice_servers: tuple[dict[str, object], ...] | None = None
    supertonic_voice_map: dict[str, str] | None = None
    tasks_url: str = "http://127.0.0.1:8080/tasks"
    delegation_db_path: str = ".opengpt-voice-delegations.sqlite3"
    vad_pause_ms: int = 700
    gliner_task_threshold: float = 0.7
    max_session_duration_s: int = 3600

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """Load settings from environment variables and validate them."""
        values = dict(os.environ if environ is None else environ)
        prefix = "OPENGPT_VOICE_"
        raw = {key.removeprefix(prefix).lower(): value for key, value in values.items() if key.startswith(prefix)}
        defaults = cls()

        settings = cls(
            host=raw.get("host", defaults.host),
            port=_integer(raw.get("port"), defaults.port, "OPENGPT_VOICE_PORT"),
            bearer_token=raw.get("bearer_token") or None,
            silero_model_path=raw.get("silero_model_path") or None,
            whisper_model_path=raw.get("whisper_model_path") or None,
            gliner_model_path=raw.get("gliner_model_path") or None,
            gliner_device=raw.get("gliner_device", defaults.gliner_device),
            router_provider=raw.get("router_provider", defaults.router_provider),
            laya_model_path=raw.get("laya_model_path") or None,
            laya_onnx_path=raw.get("laya_onnx_path") or None,
            laya_subfolder=raw.get("laya_subfolder", defaults.laya_subfolder),
            lm_studio_base_url=raw.get("lm_studio_base_url", defaults.lm_studio_base_url),
            lm_studio_model=raw.get("lm_studio_model", defaults.lm_studio_model),
            lm_studio_reasoning_effort=raw.get("lm_studio_reasoning_effort", defaults.lm_studio_reasoning_effort),
            llm_api_key=raw.get("llm_api_key") or None,
            asr_provider=raw.get("asr_provider", defaults.asr_provider),
            asr_base_url=raw.get("asr_base_url", defaults.asr_base_url),
            asr_model=raw.get("asr_model", defaults.asr_model),
            asr_api_key=raw.get("asr_api_key") or None,
            asr_language=raw.get("asr_language") or None,
            tts_provider=raw.get("tts_provider", defaults.tts_provider),
            tts_base_url=raw.get("tts_base_url", defaults.tts_base_url),
            tts_model=raw.get("tts_model", defaults.tts_model),
            tts_api_key=raw.get("tts_api_key") or None,
            tts_response_format=raw.get("tts_response_format", defaults.tts_response_format),
            turn_provider=raw.get("turn_provider", defaults.turn_provider),
            turn_url=raw.get("turn_url", defaults.turn_url),
            turn_threshold=_number(raw.get("turn_threshold"), defaults.turn_threshold, "OPENGPT_VOICE_TURN_THRESHOLD"),
            delegation_mode=raw.get("delegation_mode", defaults.delegation_mode),
            responses_base_url=raw.get("responses_base_url", defaults.responses_base_url),
            responses_model=raw.get("responses_model", defaults.responses_model),
            responses_api_key=raw.get("responses_api_key") or None,
            webrtc_ice_servers=_ice_servers(raw.get("webrtc_ice_servers")),
            supertonic_voice_map=_voice_map(raw.get("supertonic_voice_map")),
            tasks_url=raw.get("tasks_url", defaults.tasks_url),
            delegation_db_path=raw.get("delegation_db_path", defaults.delegation_db_path),
            vad_pause_ms=_integer(raw.get("vad_pause_ms"), defaults.vad_pause_ms, "OPENGPT_VOICE_VAD_PAUSE_MS"),
            gliner_task_threshold=_number(raw.get("gliner_task_threshold"), defaults.gliner_task_threshold, "OPENGPT_VOICE_GLINER_TASK_THRESHOLD"),
            max_session_duration_s=_integer(raw.get("max_session_duration_s"), defaults.max_session_duration_s, "OPENGPT_VOICE_MAX_SESSION_DURATION_S"),
        )
        return settings.validate()

    def validate(self) -> "Settings":
        """Return this settings object or raise a useful configuration error."""
        if self.router_provider not in {"gliner", "laya", "heuristic"}:
            raise ConfigurationError("OPENGPT_VOICE_ROUTER_PROVIDER must be gliner or laya or heuristic")
        if self.asr_provider not in {"faster_whisper", "openai"}:
            raise ConfigurationError("OPENGPT_VOICE_ASR_PROVIDER must be faster_whisper or openai")
        if self.tts_provider not in {"supertonic", "openai"}:
            raise ConfigurationError("OPENGPT_VOICE_TTS_PROVIDER must be supertonic or openai")
        if self.turn_provider not in {"vad", "http"}:
            raise ConfigurationError("OPENGPT_VOICE_TURN_PROVIDER must be vad or http")
        if self.delegation_mode not in {"client", "responses"}:
            raise ConfigurationError("OPENGPT_VOICE_DELEGATION_MODE must be client or responses")
        required = {
            "OPENGPT_VOICE_SILERO_MODEL_PATH": self.silero_model_path,
        }
        if self.asr_provider == "faster_whisper":
            required["OPENGPT_VOICE_WHISPER_MODEL_PATH"] = self.whisper_model_path
        else:
            required["OPENGPT_VOICE_ASR_BASE_URL"] = self.asr_base_url
            required["OPENGPT_VOICE_ASR_MODEL"] = self.asr_model
        if self.tts_provider == "openai":
            required["OPENGPT_VOICE_TTS_BASE_URL"] = self.tts_base_url
            required["OPENGPT_VOICE_TTS_MODEL"] = self.tts_model
        if self.turn_provider == "http":
            required["OPENGPT_VOICE_TURN_URL"] = self.turn_url
        if self.delegation_mode == "responses":
            required["OPENGPT_VOICE_RESPONSES_BASE_URL"] = self.responses_base_url
            required["OPENGPT_VOICE_RESPONSES_MODEL"] = self.responses_model
        if self.router_provider == "gliner":
            required["OPENGPT_VOICE_GLINER_MODEL_PATH"] = self.gliner_model_path
        elif self.router_provider == "laya":
            required["OPENGPT_VOICE_LAYA_MODEL_PATH"] = self.laya_model_path
            required["OPENGPT_VOICE_LAYA_ONNX_PATH"] = self.laya_onnx_path
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ConfigurationError(f"Missing required configuration: {', '.join(missing)}")
        if not 1 <= self.port <= 65535:
            raise ConfigurationError("OPENGPT_VOICE_PORT must be between 1 and 65535")
        if self.vad_pause_ms < 0:
            raise ConfigurationError("OPENGPT_VOICE_VAD_PAUSE_MS must not be negative")
        if not 0 <= self.gliner_task_threshold <= 1:
            raise ConfigurationError("OPENGPT_VOICE_GLINER_TASK_THRESHOLD must be between 0 and 1")
        if self.max_session_duration_s <= 0:
            raise ConfigurationError("OPENGPT_VOICE_MAX_SESSION_DURATION_S must be positive")
        if not self.delegation_db_path.strip():
            raise ConfigurationError("OPENGPT_VOICE_DELEGATION_DB_PATH must not be empty")
        return self


def _integer(value: str | None, default: int, name: str) -> int:
    """Parse an integer setting or return its default."""
    try:
        return default if value is None else int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _number(value: str | None, default: float, name: str) -> float:
    """Parse a numeric setting or return its default."""
    try:
        return default if value is None else float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc


def _ice_servers(value: str | None) -> tuple[dict[str, object], ...] | None:
    """Parse an optional JSON array of WebRTC ICE servers."""
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigurationError("OPENGPT_VOICE_WEBRTC_ICE_SERVERS must be valid JSON") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise ConfigurationError("OPENGPT_VOICE_WEBRTC_ICE_SERVERS must be a JSON array of objects")
    return tuple(parsed)


def _voice_map(value: str | None) -> dict[str, str] | None:
    """Parse the optional JSON voice map or return the default mapping."""
    if value is None:
        return {"marin": "F1"}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigurationError("OPENGPT_VOICE_SUPERTONIC_VOICE_MAP must be valid JSON") from exc
    if not isinstance(parsed, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()):
        raise ConfigurationError("OPENGPT_VOICE_SUPERTONIC_VOICE_MAP must be a JSON object of strings")
    return parsed
