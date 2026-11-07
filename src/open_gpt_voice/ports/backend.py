"""Delegated backend port for Responses-style work.

With ``delegation.type == "responses"`` the live session hands a request to a
backend model or agent and forwards its streamed events to the client inside
``response.event`` envelopes. The port keeps that backend replaceable: an
OpenAI-compatible Responses endpoint, a local agent, or a test double.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class BackendMessage:
    """One conversation message supplied to the backend."""

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class BackendRequest:
    """Context and configuration for one delegated request."""

    model: str
    transcript: str
    instructions: Sequence[str] = field(default_factory=tuple)
    history: Sequence[BackendMessage] = field(default_factory=tuple)
    tools: Sequence[Mapping[str, Any]] = field(default_factory=tuple)


class Backend(Protocol):
    """Run one delegated request and stream Responses-shaped events."""

    def stream(self, request: BackendRequest) -> AsyncIterator[Mapping[str, Any]]:
        """Yield events with a ``type`` field, such as output text deltas."""
        ...
