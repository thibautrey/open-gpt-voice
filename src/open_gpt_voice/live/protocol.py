"""Parsing and serialization for the slice-1 GPT-Live event contract."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
from dataclasses import dataclass, field
from typing import Any, Mapping


SUPPORTED_CLIENT_EVENTS = frozenset(
    {
        "session.start",
        "session.update",
        "session.input_audio.append",
        "session.input_audio.mute",
        "session.input_audio.unmute",
        "session.instructions.append",
        "session.thinking.append",
        "session.commentary.append",
        "response.item.create",
        "response.create",
        "session.close",
    }
)
APPEND_EVENTS = frozenset(
    {
        "session.instructions.append",
        "session.thinking.append",
        "session.commentary.append",
    }
)


class ProtocolError(ValueError):
    """A client event that cannot be accepted by the protocol."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        param: str | None = None,
        client_event_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.param = param
        self.client_event_id = client_event_id

    def as_error(self) -> "ServerEvent":
        error: dict[str, Any] = {
            "type": "invalid_request_error",
            "code": self.code,
            "message": self.message,
        }
        if self.param is not None:
            error["param"] = self.param
        if self.client_event_id is not None:
            error["client_event_id"] = self.client_event_id
        return ServerEvent("error", {"error": error})


@dataclass(frozen=True, slots=True)
class ClientEvent:
    """A validated client event with unsupported fields preserved but ignored."""

    type: str
    event_id: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ServerEvent:
    """A server event ready to send over the WebSocket."""

    type: str
    data: Mapping[str, Any] = field(default_factory=dict)
    event_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        event: dict[str, Any] = {"type": self.type}
        if self.event_id is not None:
            event["event_id"] = self.event_id
        event.update(self.data)
        return event

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), separators=(",", ":"))


def parse_client_event(raw: str | bytes) -> ClientEvent:
    """Parse and validate one client JSON event."""
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid_value", "Event must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ProtocolError("invalid_value", "Event must be a JSON object")

    event_type = value.get("type")
    if not isinstance(event_type, str) or event_type not in SUPPORTED_CLIENT_EVENTS:
        raise ProtocolError("invalid_value", "Unknown or missing event type", param="type")
    event_id = value.get("event_id")
    if event_id is not None and not isinstance(event_id, str):
        raise ProtocolError("invalid_value", "event_id must be a string", param="event_id")

    data = {key: item for key, item in value.items() if key not in {"type", "event_id"}}
    if event_type == "session.start":
        _validate_start(data, event_id)
    elif event_type == "session.update":
        _validate_update(data, event_id)
    elif event_type == "session.input_audio.append":
        _validate_audio(data, event_id)
    elif event_type in APPEND_EVENTS:
        _validate_append(data, event_id)

    return ClientEvent(event_type, event_id, data)


def authenticate(authorization: str | None, expected_token: str) -> bool:
    """Return whether an Authorization header contains the configured bearer token."""
    scheme, separator, token = (authorization or "").partition(" ")
    return bool(
        separator
        and scheme.lower() == "bearer"
        and token
        and hmac.compare_digest(token, expected_token)
    )


def _validate_start(data: Mapping[str, Any], event_id: str | None) -> None:
    validate_session_config(data, event_id)


def validate_session_config(
    data: Mapping[str, Any], event_id: str | None = None
) -> dict[str, Any]:
    """Validate a startup configuration and return its normalized fields.

    Accepts the WebSocket ``session.start`` payload and the ``session`` object
    posted to ``POST /v1/live/sessions`` for WebRTC.
    """
    if data.get("model") != "gpt-live-1":
        _fail("invalid_value", "model must be gpt-live-1", "model", event_id)
    delegation = data.get("delegation") or {"type": "client"}
    if not isinstance(delegation, dict):
        _fail("invalid_value", "delegation must be an object", "delegation", event_id)
    mode = delegation.get("type", "client")
    if mode not in {"client", "responses"}:
        _fail(
            "invalid_value",
            "delegation.type must be client or responses",
            "delegation.type",
            event_id,
        )
    responses = delegation.get("responses")
    if responses is not None and not isinstance(responses, dict):
        _fail("invalid_value", "delegation.responses must be an object", "delegation.responses", event_id)
    instructions = data.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        _fail("invalid_value", "instructions must be a string", "instructions", event_id)
    history = data.get("input")
    if history is not None and not isinstance(history, list):
        _fail("invalid_value", "input must be an array", "input", event_id)
    _validate_audio_format(data.get("audio"), event_id)
    resolved_delegation: dict[str, Any] = {"type": mode}
    if isinstance(responses, dict):
        resolved_delegation["responses"] = responses
    return {
        "model": data["model"],
        "delegation": resolved_delegation,
        "audio_format": _normalized_audio_format(data.get("audio")),
        "instructions": instructions or "",
        "history": history or [],
        "voice": _voice(data),
    }


def _validate_update(data: Mapping[str, Any], event_id: str | None) -> None:
    changed = data.get("session", data)
    if not isinstance(changed, dict):
        _fail("invalid_value", "session must be an object", "session", event_id)
    for field_name in ("model", "instructions", "audio"):
        if field_name in changed:
            _fail(
                "immutable_field_update",
                f"{field_name} cannot be changed after session.start",
                f"session.{field_name}",
                event_id,
            )
    delegation = changed.get("delegation")
    if delegation is not None:
        _fail(
            "immutable_field_update",
            "delegation cannot be changed after session.start",
            "session.delegation",
            event_id,
        )


def _validate_append(data: Mapping[str, Any], event_id: str | None) -> None:
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        _fail("invalid_value", "text must be a non-empty string", "text", event_id)
    if len(text.split()) > 500:
        _fail("invalid_value", "text must contain at most 500 tokens", "text", event_id)
    delegation_id = data.get("delegation_id")
    if delegation_id is not None and not isinstance(delegation_id, str):
        _fail("invalid_value", "delegation_id must be a string or null", "delegation_id", event_id)


def _validate_audio(data: Mapping[str, Any], event_id: str | None) -> None:
    encoded = data.get("audio")
    if not isinstance(encoded, str):
        _fail("invalid_audio", "audio must be base64 PCM16 bytes", "audio", event_id)
    try:
        audio = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProtocolError("invalid_audio", "audio must be valid base64", param="audio", client_event_id=event_id) from exc
    if not audio or len(audio) % 2:
        _fail("invalid_audio", "audio must contain PCM16 bytes", "audio", event_id)


def _validate_audio_format(audio: Any, event_id: str | None) -> None:
    """Validate supplied audio formats against the fixed 24 kHz mono contract.

    GPT-Live documents ``{"type": "audio/pcm", "rate": 24000}``; the original
    WebSocket slice used ``{"encoding": "pcm16le", "sample_rate": 24000,
    "channels": 1}``. Both are accepted and normalized internally.
    """
    if audio is None:
        return
    if not isinstance(audio, dict):
        _fail("invalid_value", "audio must be an object", "audio", event_id)
    formats = [audio]
    formats.extend(value for value in audio.values() if isinstance(value, dict))
    found_format = False
    for value in formats:
        audio_format = value.get("format")
        if audio_format is None:
            continue
        found_format = True
        if not isinstance(audio_format, dict):
            _fail("invalid_value", "audio.format must be an object", "audio.format", event_id)
        if audio_format.get("type") == "audio/pcm":
            if audio_format.get("rate") != 24000:
                _fail("invalid_value", "audio.format.rate must be 24000", "audio.format.rate", event_id)
            continue
        expected = {"encoding": "pcm16le", "sample_rate": 24000, "channels": 1}
        for key, required in expected.items():
            if key not in audio_format:
                _fail("invalid_value", f"audio.format.{key} is required", f"audio.format.{key}", event_id)
            if audio_format[key] != required:
                _fail("invalid_value", f"audio.format.{key} must be {required}", f"audio.format.{key}", event_id)
    if not found_format:
        _fail("invalid_value", "audio.format is required when audio is supplied", "audio.format", event_id)


def _normalized_audio_format(audio: Any) -> dict[str, Any]:
    """Return the canonical GPT-Live PCM format advertised in session.started."""
    return {"type": "audio/pcm", "rate": 24000}


def _voice(data: Mapping[str, Any]) -> str:
    """Resolve the requested voice from either supported payload shape."""
    audio = data.get("audio")
    if isinstance(audio, dict):
        output = audio.get("output")
        if isinstance(output, dict) and isinstance(output.get("voice"), str) and output["voice"]:
            return output["voice"]
    voice = data.get("voice")
    if isinstance(voice, str) and voice:
        return voice
    return "marin"


def _fail(code: str, message: str, param: str, event_id: str | None) -> None:
    raise ProtocolError(code, message, param=param, client_event_id=event_id)
