"""FastAPI composition root for the OpenGPT-Voice server."""

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from loguru import logger

from .adapters.sqlite_delegations import SQLiteDelegations
from .config import Settings
from .ports import ASR, Backend, Router, Speaker, Tasks, Talker, TurnDetector, VAD


_MAX_CALLBACK_BODY_BYTES = 64 * 1024
_MAX_CALLBACK_WORDS = 500
_CALLBACK_WAIT_SECONDS = 5
_MAX_SESSION_BODY_BYTES = 512 * 1024
_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Ports:
    """The replaceable collaborators used by one server process."""

    vad: VAD
    asr: ASR
    router: Router
    talker: Talker
    speaker: Speaker
    tasks: Tasks
    turn: TurnDetector | None = None
    backend: Backend | None = None


def create_app(
    settings: Settings,
    ports_factory: Callable[[], Ports | None] | None = None,
    delegations: SQLiteDelegations | None = None,
) -> FastAPI:
    """Create the FastAPI application with validated settings and injected ports."""
    settings.validate()
    callback_store = delegations or SQLiteDelegations(
        settings.delegation_db_path,
        session_ttl_seconds=settings.max_session_duration_s,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Initializing delegation store", database_path=settings.delegation_db_path)
        await asyncio.to_thread(callback_store.initialize)
        worker = asyncio.create_task(_dispatch_callbacks(app, callback_store))
        try:
            yield
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await asyncio.to_thread(callback_store.close_worker)
            logger.info("Delegation callback worker stopped")

    app = FastAPI(title="OpenGPT-Voice server", lifespan=lifespan)
    app.state.settings = settings
    app.state.ports_factory = ports_factory or (lambda: None)
    app.state.sessions = {}
    app.state.session_tasks = {}
    app.state.delegations = callback_store

    def is_authorized(authorization: str | None) -> bool:
        from .live.protocol import authenticate

        return not settings.bearer_token or authenticate(authorization, settings.bearer_token)

    async def run_live_session(websocket: WebSocket) -> None:
        from .live.session import LiveSession

        session = LiveSession(websocket, settings, app.state.ports_factory)
        app.state.sessions[session.state.session_id] = session
        logger.info("Live WebSocket accepted", session_id=session.state.session_id, active_sessions=len(app.state.sessions))
        try:
            await session.run()
        except WebSocketDisconnect:
            await session.close("connection_lost")
        finally:
            app.state.sessions.pop(session.state.session_id, None)
            await _unregister_session(callback_store, session.state.session_id)
            logger.info("Live WebSocket removed", session_id=session.state.session_id, active_sessions=len(app.state.sessions))

    @app.get("/", include_in_schema=False)
    def voice_demo() -> FileResponse:
        return FileResponse(Path(__file__).resolve().parents[2] / "demo" / "index.html")

    @app.get("/status", include_in_schema=False, response_class=HTMLResponse)
    def demo_status() -> str:
        return '<p id="configuration">OpenGPT-Voice server connected</p>'

    @app.get("/health")
    def health() -> dict[str, str]:
        """Return a minimal liveness response."""
        return {"status": "ok"}

    @app.post("/internal/delegations/{delegation_id}/result")
    async def delegation_result(delegation_id: str, request: Request) -> dict[str, str]:
        """Deliver task callback text to its live session for speech output."""
        if not is_authorized(request.headers.get("authorization")):
            raise HTTPException(status_code=401, detail="Unauthorized")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > _MAX_CALLBACK_BODY_BYTES:
                raise HTTPException(status_code=413, detail="callback body is too large")
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="request body must be valid JSON") from exc
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(status_code=422, detail="content must be a non-empty string")
        if len(content.split()) > _MAX_CALLBACK_WORDS:
            raise HTTPException(status_code=422, detail="content must contain at most 500 words")
        try:
            callback_id = await asyncio.to_thread(callback_store.enqueue, delegation_id, content)
        except sqlite3.Error as exc:
            raise HTTPException(status_code=503, detail="Callback store unavailable") from exc
        if callback_id is None:
            logger.warning("Callback rejected for unknown delegation", delegation_id=delegation_id)
            raise HTTPException(status_code=404, detail="Unknown delegation")
        deadline = monotonic() + _CALLBACK_WAIT_SECONDS
        while monotonic() < deadline:
            try:
                accepted = await asyncio.to_thread(callback_store.result, callback_id)
            except sqlite3.Error as exc:
                raise HTTPException(status_code=503, detail="Callback store unavailable") from exc
            if accepted is not None:
                if accepted:
                    logger.info("Delegation callback accepted", delegation_id=delegation_id)
                    return {"status": "accepted"}
                raise HTTPException(status_code=404, detail="Unknown delegation")
            await asyncio.sleep(0.05)
        raise HTTPException(status_code=503, detail="Session worker did not acknowledge callback")

    @app.websocket("/v1/live/sessions")
    async def live_sessions(websocket: WebSocket) -> None:
        """Authenticate and run one isolated OpenGPT-Voice session."""
        if not is_authorized(websocket.headers.get("authorization")):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        await run_live_session(websocket)

    @app.post("/v1/live/sessions")
    async def create_webrtc_session(request: Request) -> dict[str, object]:
        """Create a WebRTC session from an SDP offer, as GPT-Live documents it."""
        if not is_authorized(request.headers.get("authorization")):
            raise HTTPException(status_code=401, detail="Unauthorized")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > _MAX_SESSION_BODY_BYTES:
                raise HTTPException(status_code=413, detail="session body is too large")
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="request body must be a JSON object")
        session_config = payload.get("session")
        transport = payload.get("transport")
        if not isinstance(session_config, dict):
            raise HTTPException(status_code=400, detail="session must be an object")
        if not isinstance(transport, dict) or transport.get("type") != "webrtc":
            raise HTTPException(status_code=400, detail="transport.type must be webrtc")
        offer_sdp = transport.get("sdp")
        if not isinstance(offer_sdp, str) or not offer_sdp.strip():
            raise HTTPException(status_code=400, detail="transport.sdp must be a non-empty string")
        from .live.protocol import ProtocolError, validate_session_config

        try:
            validate_session_config(session_config)
        except ProtocolError as error:
            raise HTTPException(
                status_code=422,
                detail={"code": error.code, "message": error.message, "param": error.param},
            ) from error
        try:
            from .transports.webrtc import WebRTCTransport
        except ImportError as exc:
            raise HTTPException(
                status_code=503,
                detail="WebRTC transport requires the optional aiortc dependency",
            ) from exc
        try:
            webrtc = await WebRTCTransport.create(offer_sdp, settings.webrtc_ice_servers)
        except Exception as exc:
            logger.warning("WebRTC offer rejected", error=str(exc))
            raise HTTPException(status_code=400, detail="invalid WebRTC offer") from exc

        from .live.session import LiveSession

        session_id = f"sess_{uuid4().hex}"
        session = LiveSession(
            webrtc,
            settings,
            app.state.ports_factory,
            session_id=session_id,
            session_config=session_config,
        )
        app.state.sessions[session_id] = session
        logger.info(
            "WebRTC live session created",
            session_id=session_id,
            active_sessions=len(app.state.sessions),
        )
        task = asyncio.create_task(_run_webrtc_session(app, session, callback_store))
        app.state.session_tasks[session_id] = task
        return {
            "session": {"id": session_id, "model": session_config["model"]},
            "transport": {"type": "webrtc", "sdp": webrtc.answer_sdp},
        }

    @app.websocket("/v1/live/sessions/{session_id}/attach")
    async def attach_sideband(websocket: WebSocket, session_id: str) -> None:
        """Attach a server-side sideband socket to a running session."""
        if not is_authorized(websocket.headers.get("authorization")):
            await websocket.close(code=1008)
            return
        session = app.state.sessions.get(session_id)
        if session is None:
            await websocket.close(code=1008, reason="Unknown session")
            return
        await websocket.accept()
        queue = session.subscribe()
        await websocket.send_text(
            json.dumps(
                {
                    "type": "session.attached",
                    "session": {"id": session_id},
                },
                separators=(",", ":"),
            )
        )

        async def pump_events() -> None:
            while True:
                await websocket.send_text(await queue.get())

        async def pump_commands() -> None:
            while True:
                await session.receive_command(await websocket.receive_text())

        tasks = [asyncio.create_task(pump_events()), asyncio.create_task(pump_commands())]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exception = task.exception()
                if exception is not None and not isinstance(exception, WebSocketDisconnect):
                    logger.warning("Sideband closed with an error", error=str(exception))
        finally:
            session.unsubscribe(queue)
            logger.info("Sideband detached", session_id=session_id)

    @app.websocket("/ws/live")
    async def browser_live(websocket: WebSocket) -> None:
        """Authenticate the browser UI then run the same live session handler."""
        from .live.protocol import ServerEvent

        await websocket.accept()
        try:
            auth_frame = await websocket.receive_text()
            if len(auth_frame) > 4096:
                await websocket.close(code=1008, reason="Invalid authentication frame")
                return
            payload = json.loads(auth_frame)
            token = payload.get("token") if isinstance(payload, dict) and payload.get("type") == "auth" else None
            authorization = f"Bearer {token}" if isinstance(token, str) and token else None
            if not is_authorized(authorization):
                logger.warning("Browser WebSocket authentication rejected")
                await websocket.close(code=1008, reason="Unauthorized")
                return
            await websocket.send_text(ServerEvent("session.authenticated").to_json())
            await run_live_session(websocket)
        except (WebSocketDisconnect, json.JSONDecodeError):
            return

    return app


async def _unregister_session(callback_store: SQLiteDelegations, session_id: str) -> None:
    """Drop queued callbacks for a finished session, tolerating a closed store."""
    try:
        await asyncio.to_thread(callback_store.unregister_session, session_id)
    except Exception:
        _logger.warning("Unable to unregister session callbacks for %s", session_id)


async def _run_webrtc_session(app: FastAPI, session: Any, callback_store: SQLiteDelegations) -> None:
    """Run one WebRTC session and clean its registry entries on exit."""
    try:
        await session.run()
    except Exception:
        _logger.exception("WebRTC session failed")
        await session.close("internal_error")
    finally:
        app.state.sessions.pop(session.state.session_id, None)
        app.state.session_tasks.pop(session.state.session_id, None)
        await _unregister_session(callback_store, session.state.session_id)


async def _dispatch_callbacks(app: FastAPI, callback_store: SQLiteDelegations) -> None:
    """Poll the local SQLite queue and deliver callbacks to sessions owned here."""
    last_heartbeat = 0.0
    accepted_callbacks: set[str] = set()
    while True:
        try:
            if monotonic() - last_heartbeat >= 5:
                await asyncio.to_thread(callback_store.heartbeat)
                last_heartbeat = monotonic()
            callbacks = await asyncio.to_thread(callback_store.pending)
            accepted_callbacks.intersection_update(callback[0] for callback in callbacks)
            for callback_id, delegation_id, content in callbacks:
                accepted = callback_id in accepted_callbacks
                if not accepted:
                    accepted = any(
                        session.handle_task_result(delegation_id, content)
                        for session in app.state.sessions.values()
                    )
                    if accepted:
                        accepted_callbacks.add(callback_id)
                await asyncio.to_thread(callback_store.complete, callback_id, accepted)
                accepted_callbacks.discard(callback_id)
        except Exception:
            _logger.exception("Delegation callback polling failed")
        await asyncio.sleep(0.05)
