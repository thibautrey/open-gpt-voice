"""Dependency-injection boundaries for the local model pipeline."""

from .asr import ASR, Transcript
from .backend import Backend, BackendMessage, BackendRequest
from .router import RouteDecision, Router
from .speaker import Speaker
from .tasks import Tasks
from .talker import TalkRequest, Talker, TranscriptTurn
from .turn import TurnDecision, TurnDetector
from .vad import VAD, VADEvent


__all__ = ["ASR", "Backend", "BackendMessage", "BackendRequest", "RouteDecision", "Router", "Speaker", "TalkRequest", "Tasks", "Talker", "Transcript", "TranscriptTurn", "TurnDecision", "TurnDetector", "VAD", "VADEvent"]
