"""Tests for the GPT-Live compatible HTTP/WebRTC surface."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from open_gpt_voice.app import Ports, create_app
from open_gpt_voice.config import Settings
from open_gpt_voice.live.protocol import ProtocolError, validate_session_config

_SETTINGS = Settings(
    silero_model_path="/models/silero.onnx",
    whisper_model_path="/models/whisper",
    gliner_model_path="/models/gliner",
    bearer_token="secret",
)


class _FakeWebRTC:
    answer_sdp = "v=0\r\n"

    @classmethod
    async def create(cls, offer_sdp: str, ice_servers=None):
        return cls()

    async def send_text(self, text: str) -> None:
        return None


def _ports() -> Ports:
    return Ports(
        vad=SimpleNamespace(),
        asr=SimpleNamespace(),
        router=SimpleNamespace(),
        talker=SimpleNamespace(),
        speaker=SimpleNamespace(),
        tasks=SimpleNamespace(),
    )


class SessionConfigTests(unittest.TestCase):
    def test_responses_delegation_is_accepted(self) -> None:
        resolved = validate_session_config(
            {
                "model": "gpt-live-1",
                "delegation": {"type": "responses", "responses": {"model": "qwen3"}},
                "audio": {"format": {"type": "audio/pcm", "rate": 24000}},
            }
        )

        self.assertEqual(resolved["delegation"]["type"], "responses")
        self.assertEqual(resolved["delegation"]["responses"], {"model": "qwen3"})

    def test_unknown_delegation_type_is_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            validate_session_config({"model": "gpt-live-1", "delegation": {"type": "server"}})

    def test_legacy_audio_format_is_normalized(self) -> None:
        resolved = validate_session_config(
            {
                "model": "gpt-live-1",
                "audio": {"format": {"encoding": "pcm16le", "sample_rate": 24000, "channels": 1}},
            }
        )

        self.assertEqual(resolved["audio_format"], {"type": "audio/pcm", "rate": 24000})


class WebRTCSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app(_SETTINGS, _ports)
        self.client = TestClient(self.app)

    def test_requires_bearer_token(self) -> None:
        response = self.client.post("/v1/live/sessions", json={})

        self.assertEqual(response.status_code, 401)

    def test_rejects_malformed_body(self) -> None:
        response = self.client.post(
            "/v1/live/sessions",
            content=b"{not json",
            headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
        )

        self.assertEqual(response.status_code, 400)

    def test_rejects_non_webrtc_transport(self) -> None:
        response = self.client.post(
            "/v1/live/sessions",
            json={
                "session": {"model": "gpt-live-1"},
                "transport": {"type": "websocket"},
            },
            headers={"Authorization": "Bearer secret"},
        )

        self.assertEqual(response.status_code, 400)

    def test_rejects_invalid_session_config(self) -> None:
        response = self.client.post(
            "/v1/live/sessions",
            json={
                "session": {"model": "gpt-live-2"},
                "transport": {"type": "webrtc", "sdp": "v=0\r\n"},
            },
            headers={"Authorization": "Bearer secret"},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "invalid_value")

    def test_creates_session_with_webrtc_transport(self) -> None:
        async def _noop(self) -> None:  # pragma: no cover - exercised via task
            return None

        with patch(
            "open_gpt_voice.transports.webrtc.WebRTCTransport.create", _FakeWebRTC.create
        ), patch("open_gpt_voice.live.session.LiveSession.run", _noop):
            response = self.client.post(
                "/v1/live/sessions",
                json={
                    "session": {
                        "model": "gpt-live-1",
                        "delegation": {"type": "responses"},
                    },
                    "transport": {"type": "webrtc", "sdp": "v=0\r\n"},
                },
                headers={"Authorization": "Bearer secret"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["session"]["model"], "gpt-live-1")
        self.assertEqual(payload["transport"], {"type": "webrtc", "sdp": "v=0\r\n"})
        self.assertEqual(len(self.app.state.sessions), 0)

    def test_attach_rejects_unknown_session(self) -> None:
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(
                "/v1/live/sessions/sess_missing/attach",
                headers={"Authorization": "Bearer secret"},
            ) as websocket:
                websocket.receive_text()


class ResponsesDelegationTests(unittest.TestCase):
    def test_backend_stream_is_relayed_as_response_events(self) -> None:
        from open_gpt_voice.live.session import LiveSession
        from open_gpt_voice.ports.router import RouteDecision

        class Router:
            def classify(self, transcript: str) -> RouteDecision:
                return RouteDecision(transcript, 0.9, "task", True, {"kind": "task"})

        class Backend:
            async def stream(self, request):
                yield {"type": "response.created", "response": {"id": "resp_1"}}
                yield {"type": "response.output_text.delta", "delta": "Bonjour. "}
                yield {"type": "response.output_text.delta", "delta": "Voici le résultat."}
                yield {"type": "response.completed"}

        class Websocket:
            def __init__(self) -> None:
                self.sent: list[dict] = []

            async def send_text(self, text: str) -> None:
                self.sent.append(json.loads(text))

        class Talker:
            async def reply(self, request):
                return "Je m'en occupe."

        websocket = Websocket()
        session = LiveSession(websocket, _SETTINGS, _ports)
        session.ports = SimpleNamespace(
            router=Router(), backend=Backend(), speaker=object(), talker=Talker()
        )
        session.state.delegation_mode = "responses"
        session.state.delegation_config = {"type": "responses"}

        asyncio.run(_run_delegation(session))

        created = [event for event in websocket.sent if event["type"] == "session.delegation.created"]
        relayed = [event for event in websocket.sent if event["type"] == "response.event"]
        self.assertEqual(created[0]["delegation"]["target"], "responses")
        self.assertTrue(created[0]["delegation"]["response_id"].startswith("resp_"))
        self.assertEqual([event["event"]["type"] for event in relayed][-1], "response.completed")
        self.assertEqual(session.state.history[-1].text, "Bonjour. Voici le résultat.")


async def _run_delegation(session) -> None:
    await session.handle_transcript("Fais un calcul")
    await asyncio.gather(*session._background_tasks)


class WebRTCTransportTests(unittest.TestCase):
    def test_already_open_data_channel_unblocks_send(self) -> None:
        try:
            from open_gpt_voice.transports.webrtc import WebRTCTransport
        except ImportError:  # pragma: no cover - aiortc is optional
            self.skipTest("aiortc is not installed")

        class Channel:
            label = "oai-events"
            readyState = "open"

            def __init__(self) -> None:
                self.sent: list[str] = []

            def on(self, event: str, handler) -> None:
                return None

            def send(self, data: str) -> None:
                self.sent.append(data)

        channel = Channel()
        transport = WebRTCTransport(SimpleNamespace(on=lambda *args: None))
        transport._on_datachannel(channel)

        asyncio.run(transport.send_text('{"type":"session.started"}'))

        self.assertEqual(channel.sent, ['{"type":"session.started"}'])

    def test_missing_data_channel_does_not_block_sideband_only_session(self) -> None:
        try:
            from open_gpt_voice.transports.webrtc import WebRTCTransport
        except ImportError:  # pragma: no cover - aiortc is optional
            self.skipTest("aiortc is not installed")

        transport = WebRTCTransport(SimpleNamespace(on=lambda *args: None))

        async def _send() -> None:
            await asyncio.wait_for(transport.send_text('{"type":"session.started"}'), timeout=0.5)

        asyncio.run(_send())


class SidebandTests(unittest.TestCase):
    def test_subscribe_receives_broadcast(self) -> None:
        from open_gpt_voice.live.session import LiveSession

        session = LiveSession(_FakeWebRTC(), _SETTINGS, _ports)
        queue = session.subscribe()

        async def _broadcast() -> None:
            await session._send(
                __import__("open_gpt_voice.live.protocol", fromlist=["ServerEvent"]).ServerEvent(
                    "test.event", {}
                )
            )

        asyncio.run(_broadcast())
        self.assertIn("test.event", queue.get_nowait())
        session.unsubscribe(queue)


if __name__ == "__main__":
    unittest.main()
