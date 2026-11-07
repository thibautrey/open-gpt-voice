"""WebRTC transport for GPT-Live sessions.

The browser creates an SDP offer and a data channel named ``oai-events``. The
application server answers through ``POST /v1/live/sessions``; audio then travels
on the media tracks while JSON events travel on the data channel. This module
adapts that pair to the same ``receive_text``/``send_text`` surface the
WebSocket session loop already uses, so the orchestration layer stays
transport-agnostic.

Audio contract:
- input track: any WebRTC sample rate, downmixed to mono and resampled to
  24 kHz PCM16LE, re-emitted as ``session.input_audio.append`` events.
- output track: 24 kHz PCM16LE from ``session.output_audio.delta`` events,
  upsampled to 48 kHz for Opus.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.mediastreams import AudioFrame, MediaStreamError, MediaStreamTrack
from loguru import logger

_SESSION_RATE = 24_000
_WEBRTC_RATE = 48_000
_FRAME_SAMPLES = 960  # 20 ms at 48 kHz
_ICE_GATHERING_TIMEOUT_S = 5.0
_DATA_CHANNEL_LABEL = "oai-events"


class WebRTCAudioOutput(MediaStreamTrack):
    """Media track that plays assistant audio queued by the session layer."""

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._buffer = np.zeros(0, dtype=np.int16)
        self._closed = False

    def push(self, pcm16le_24k: bytes) -> None:
        """Append one 24 kHz mono PCM16LE chunk to the playback buffer."""
        samples = np.frombuffer(pcm16le_24k, dtype=np.int16)
        if samples.size == 0:
            return
        # Simple 2x upsample: adequate for Opus at 48 kHz and cheaper than a
        # polyphase filter on every 100 ms audio delta.
        self._buffer = np.concatenate((self._buffer, np.repeat(samples, 2)))

    def clear(self) -> None:
        """Drop buffered audio after a barge-in."""
        self._buffer = np.zeros(0, dtype=np.int16)

    async def recv(self) -> AudioFrame:
        """Return one 20 ms frame, padding with silence to keep cadence."""
        if self._closed:
            raise MediaStreamError
        if self._buffer.size < _FRAME_SAMPLES:
            padding = np.zeros(_FRAME_SAMPLES - self._buffer.size, dtype=np.int16)
            self._buffer = np.concatenate((self._buffer, padding))
        chunk = self._buffer[:_FRAME_SAMPLES]
        self._buffer = self._buffer[_FRAME_SAMPLES:]
        await asyncio.sleep(_FRAME_SAMPLES / _WEBRTC_RATE)
        return AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")

    def stop(self) -> None:
        """Stop the track and release buffered audio."""
        self._closed = True
        self._buffer = np.zeros(0, dtype=np.int16)
        super().stop()


class WebRTCTransport:
    """Bridge one WebRTC peer connection to the session event loop."""

    def __init__(self, peer_connection: RTCPeerConnection) -> None:
        self._pc = peer_connection
        self._audio_out = WebRTCAudioOutput()
        self._incoming: asyncio.Queue[str] = asyncio.Queue()
        self._channel: Any | None = None
        self._channel_open = asyncio.Event()
        self._closed = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self._ice_complete = asyncio.Event()
        self.log = logger.bind(component="webrtc-transport")
        self._pc.on("datachannel", self._on_datachannel)
        self._pc.on("track", self._on_track)
        self._pc.on("icegatheringstatechange", self._on_ice_state)
        self._pc.on("connectionstatechange", self._on_connection_state)

    def attach_output_track(self) -> None:
        """Add the assistant audio track once the remote offer is known."""
        self._pc.addTrack(self._audio_out)

    @classmethod
    async def create(
        cls,
        offer_sdp: str,
        ice_servers: Sequence[Mapping[str, Any]] | None = None,
    ) -> "WebRTCTransport":
        """Answer one browser offer and wait for local ICE candidates."""
        configuration = RTCConfiguration(
            iceServers=[RTCIceServer(**server) for server in ice_servers or []]
        )
        peer_connection = RTCPeerConnection(configuration)
        transport = cls(peer_connection)
        await peer_connection.setRemoteDescription(
            RTCSessionDescription(sdp=offer_sdp, type="offer")
        )
        if "m=audio" in offer_sdp:
            transport.attach_output_track()
        answer = await peer_connection.createAnswer()
        await peer_connection.setLocalDescription(answer)
        try:
            await asyncio.wait_for(transport._ice_complete.wait(), _ICE_GATHERING_TIMEOUT_S)
        except asyncio.TimeoutError:
            transport.log.warning("ICE gathering timed out; answering with current candidates")
        return transport

    @property
    def answer_sdp(self) -> str:
        """Return the negotiated SDP answer."""
        description = self._pc.localDescription
        if description is None:
            raise RuntimeError("WebRTC answer is not available")
        return description.sdp

    async def receive_text(self) -> str:
        """Yield the next client event, including synthesized audio appends."""
        return await self._incoming.get()

    async def send_text(self, data: str) -> None:
        """Send an event on the data channel, routing audio deltas to the track."""
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            event = {}
        event_type = event.get("type")
        if event_type == "session.output_audio.delta":
            encoded = event.get("delta")
            if isinstance(encoded, str):
                try:
                    self._audio_out.push(base64.b64decode(encoded, validate=True))
                except (binascii.Error, ValueError):
                    self.log.warning("Dropping invalid output audio delta")
            return
        if event_type == "session.output_audio.cancelled":
            self._audio_out.clear()
        if self._closed:
            return
        await self._channel_open.wait()
        if self._channel is None or self._channel.readyState != "open":
            return
        self._channel.send(data)

    async def close(self) -> None:
        """Close the peer connection and every background task."""
        if self._closed:
            return
        self._closed = True
        self._audio_out.stop()
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._pc.close()

    def _on_datachannel(self, channel: Any) -> None:
        self._channel = channel
        channel.on("open", self._channel_open.set)
        channel.on("close", self._channel_open.clear)
        channel.on("message", self._on_message)
        self.log.info(
            "WebRTC data channel opened",
            label=getattr(channel, "label", None),
            ready_state=getattr(channel, "readyState", None),
        )
        if getattr(channel, "readyState", None) == "open":
            self._channel_open.set()
        if getattr(channel, "label", None) != _DATA_CHANNEL_LABEL:
            self.log.warning(
                "Unexpected data channel label; expected oai-events",
                label=getattr(channel, "label", None),
            )

    def _on_message(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            try:
                message = message.decode("utf-8")
            except UnicodeDecodeError:
                self.log.warning("Dropping non UTF-8 data channel message")
                return
        self._incoming.put_nowait(message)

    def _on_track(self, track: MediaStreamTrack) -> None:
        if track.kind != "audio":
            return
        task = asyncio.create_task(self._pump_input(track))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _pump_input(self, track: MediaStreamTrack) -> None:
        """Convert remote audio frames into session.input_audio.append events."""
        try:
            while not self._closed:
                frame = await track.recv()
                pcm16le = _frame_to_session_pcm(frame)
                if not pcm16le:
                    continue
                self._incoming.put_nowait(
                    json.dumps(
                        {
                            "type": "session.input_audio.append",
                            "audio": base64.b64encode(pcm16le).decode("ascii"),
                        },
                        separators=(",", ":"),
                    )
                )
        except MediaStreamError:
            self.log.info("WebRTC input track ended")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.log.exception("WebRTC input pump failed")

    def _on_ice_state(self) -> None:
        if self._pc.iceGatheringState == "complete":
            self._ice_complete.set()

    def _on_connection_state(self) -> None:
        state = self._pc.connectionState
        self.log.info("WebRTC connection state changed", state=state)
        if state in {"failed", "closed", "disconnected"} and not self._closed:
            self._incoming.put_nowait(json.dumps({"type": "session.close", "reason": "connection_lost"}))
            self._channel_open.clear()


def _frame_to_session_pcm(frame: AudioFrame) -> bytes:
    """Downmix a decoded WebRTC frame to 24 kHz mono PCM16LE."""
    array = frame.to_ndarray()
    if array.size == 0:
        return b""
    if array.ndim > 1 and array.shape[0] > 1:
        mono = array.mean(axis=0)
    else:
        mono = array.reshape(-1)
    mono = np.asarray(mono, dtype=np.float32)
    if frame.sample_rate != _SESSION_RATE and mono.size > 1:
        target = max(1, int(round(mono.size * _SESSION_RATE / frame.sample_rate)))
        mono = np.interp(
            np.linspace(0, mono.size - 1, target, dtype=np.float64),
            np.arange(mono.size, dtype=np.float64),
            mono,
        )
    return np.clip(mono, -32768, 32767).astype(np.int16).tobytes()
