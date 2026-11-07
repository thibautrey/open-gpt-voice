"""Transport adapters for the GPT-Live session layer.

WebRTC is an optional dependency: import ``WebRTCTransport`` lazily so a
WebSocket-only deployment does not need ``aiortc`` installed.
"""

__all__ = ["WebRTCTransport", "WebRTCAudioOutput"]


def __getattr__(name: str):
    if name in {"WebRTCTransport", "WebRTCAudioOutput"}:
        from . import webrtc

        return getattr(webrtc, name)
    raise AttributeError(name)
