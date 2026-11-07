"""HTTP end-of-turn detector adapter.

Talks to a local decision model (DuplexJev or any compatible service) that
answers whether the buffered speech is a complete turn. The contract is
deliberately small: POST ``{"audio_b64", "sample_rate", "start_ms"}`` and read
``{"complete": bool, "confidence": float, "reason": str}``. When the service is
unreachable the adapter fails open (complete) so a conversation never stalls.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping

from loguru import logger

from ..ports.turn import TurnDecision

_SAMPLE_RATE = 24_000


class HttpTurnDetector:
    """Decide turn completion through a local HTTP decision service."""

    def __init__(
        self,
        url: str,
        *,
        threshold: float = 0.5,
        timeout_s: float = 2.0,
        api_key: str | None = None,
        transport: Callable[[str, bytes, Mapping[str, str]], bytes] | None = None,
    ) -> None:
        self.url = url
        self.threshold = threshold
        self.timeout_s = timeout_s
        self.api_key = api_key
        self.transport = transport
        self.log = logger.bind(component="turn-detector")

    def is_complete(self, pcm16le: bytes, start_ms: int) -> TurnDecision:
        """Return the decision service's answer, failing open on errors."""
        if not pcm16le:
            return TurnDecision(False, 0.0, "incomplete")
        payload = {
            "audio_b64": base64.b64encode(pcm16le).decode("ascii"),
            "sample_rate": _SAMPLE_RATE,
            "start_ms": start_ms,
        }
        body = json.dumps(payload).encode("utf-8")
        try:
            raw = (
                self.transport(self.url, body, self._headers())
                if self.transport is not None
                else self._post(body)
            )
            response = json.loads(raw)
        except Exception:
            self.log.warning("Turn detector unavailable; treating the turn as complete")
            return TurnDecision(True, 0.0, "silence")
        complete = bool(response.get("complete", True))
        confidence = float(response.get("confidence", 1.0))
        reason = response.get("reason")
        if reason not in {"silence", "semantic", "incomplete"}:
            reason = "semantic" if complete else "incomplete"
        if complete and confidence < self.threshold:
            complete = False
            reason = "incomplete"
        return TurnDecision(complete, confidence, reason)

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
