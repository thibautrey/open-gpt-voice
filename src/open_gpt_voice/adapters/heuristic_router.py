"""Model-free routing adapter.

A local cascade does not need a GLiNER model just to decide whether a turn is
conversation or background work. This adapter keeps that decision deterministic
and inspectable: every transcript is spoken, and turns containing an action
keyword are also delegated. It is the default profile for the minimal local
stack; GLiNER and Laya remain available for better routing.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..ports.router import BackgroundDelivery, RouteDecision, ToolProfile

_DEFAULT_KEYWORDS = (
    "remind",
    "reminder",
    "timer",
    "book",
    "booking",
    "order",
    "schedule",
    "search",
    "look up",
    "find",
    "send",
    "email",
    "report",
    "prepare",
    "rappelle",
    "minuteur",
    "réserve",
    "reserve",
    "commande",
    "cherche",
    "envoie",
    "prépare",
    "prépare",
)


class HeuristicRouter:
    """Route with keyword matching instead of a trained classifier."""

    def __init__(self, keywords: Sequence[str] | None = None, task_threshold: float = 0.5) -> None:
        self.keywords = tuple(keyword.lower() for keyword in (keywords or _DEFAULT_KEYWORDS))
        self.task_threshold = task_threshold

    def classify(self, transcript: str) -> RouteDecision:
        lowered = transcript.lower()
        matches = [keyword for keyword in self.keywords if keyword in lowered]
        score = 1.0 if matches else 0.0
        return RouteDecision(
            transcript=transcript,
            needs_task=score,
            kind="task" if matches else "chat",
            talk=True,
            labels={"keywords": matches},
            task_threshold=self.task_threshold,
        )

    def classify_background_delivery(
        self, request: str, result: str, profile: ToolProfile | None = None
    ) -> BackgroundDelivery:
        if profile is not None and profile.urgency == "urgent":
            return "interrupt_after_sentence"
        return "after_playback"
