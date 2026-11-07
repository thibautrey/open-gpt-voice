"""End-of-turn detection port.

A VAD reports silence; a turn detector decides whether that silence means the
user finished a thought or is merely pausing. Keeping this decision behind a
port lets a cascade use a semantic model (DuplexJev or similar) without changing
the session orchestration.
"""

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class TurnDecision:
    """Whether one utterance is complete enough to answer."""

    complete: bool
    confidence: float = 1.0
    reason: Literal["silence", "semantic", "incomplete"] = "silence"


class TurnDetector(Protocol):
    """Decide whether buffered speech forms a complete turn."""

    def is_complete(self, pcm16le: bytes, start_ms: int) -> TurnDecision:
        """Return the turn decision for the audio buffered so far."""
        ...
