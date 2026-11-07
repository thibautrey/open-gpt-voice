"""ASGI entry point for local development."""

from time import perf_counter

from loguru import logger

from .adapters.faster_whisper import FasterWhisperASR
from .adapters.gliner_router import GLiNERRouter
from .adapters.heuristic_router import HeuristicRouter
from .adapters.http_tasks import HttpTasks
from .adapters.http_turn import HttpTurnDetector
from .adapters.laya_router import LayaRouter
from .adapters.lmstudio_talker import LMStudioTalker
from .adapters.openai_asr import OpenAICompatibleASR
from .adapters.openai_speaker import OpenAICompatibleSpeaker
from .adapters.responses_backend import ResponsesBackend
from .adapters.silero_vad import SileroVAD
from .adapters.sqlite_delegations import SQLiteDelegations
from .adapters.supertonic_speaker import SupertonicSpeaker
from .app import Ports, create_app
from .config import Settings
from .observability import configure_logging
from .ports import Router
from .ports.turn import TurnDetector

configure_logging()
settings = Settings.from_env().validate()
logger.info(
    "Settings loaded",
    router_provider=settings.router_provider,
    asr_provider=settings.asr_provider,
    tts_provider=settings.tts_provider,
    turn_provider=settings.turn_provider,
    delegation_mode=settings.delegation_mode,
    llm_model=settings.lm_studio_model,
)

router_load_started = perf_counter()
logger.info("Loading routing model", provider=settings.router_provider)
router: Router
if settings.router_provider == "heuristic":
    router = HeuristicRouter(task_threshold=settings.gliner_task_threshold)
elif settings.router_provider == "laya":
    router = LayaRouter(
        settings.laya_model_path or "",
        onnx_path=settings.laya_onnx_path or "",
        subfolder=settings.laya_subfolder,
        task_threshold=settings.gliner_task_threshold,
    )
else:
    router = GLiNERRouter(
        settings.gliner_model_path or "",
        device=settings.gliner_device,
        task_threshold=settings.gliner_task_threshold,
    )
logger.info(
    "Routing model ready",
    provider=settings.router_provider,
    elapsed_ms=round((perf_counter() - router_load_started) * 1000),
)

talker = LMStudioTalker(
    settings.lm_studio_base_url,
    settings.lm_studio_model,
    reasoning_effort=settings.lm_studio_reasoning_effort,
    api_key=settings.llm_api_key,
)
logger.info("Talker configured", base_url=settings.lm_studio_base_url)

if settings.tts_provider == "openai":
    speaker = OpenAICompatibleSpeaker(
        settings.tts_base_url,
        settings.tts_model,
        api_key=settings.tts_api_key,
        voice_map=settings.supertonic_voice_map,
        response_format=settings.tts_response_format,
    )
else:
    speaker = SupertonicSpeaker(settings.supertonic_voice_map)
logger.info("Speaker configured", provider=settings.tts_provider)

turn: TurnDetector | None = None
if settings.turn_provider == "http":
    turn = HttpTurnDetector(settings.turn_url, threshold=settings.turn_threshold)
logger.info("Turn detector configured", provider=settings.turn_provider)

backend = None
if settings.delegation_mode == "responses":
    backend = ResponsesBackend(settings.responses_base_url, settings.responses_api_key)
logger.info("Delegation backend configured", mode=settings.delegation_mode)

delegations = SQLiteDelegations(
    settings.delegation_db_path,
    session_ttl_seconds=settings.max_session_duration_s,
)


def ports_factory() -> Ports:
    """Create stateful speech adapters for one live session."""
    logger.debug("Creating per-session VAD and ASR adapters", asr=settings.asr_provider)
    if settings.asr_provider == "openai":
        asr = OpenAICompatibleASR(
            settings.asr_base_url,
            settings.asr_model,
            api_key=settings.asr_api_key,
            language=settings.asr_language,
        )
    else:
        asr = FasterWhisperASR(settings.whisper_model_path or "")
    return Ports(
        vad=SileroVAD(settings.silero_model_path or "", pause_ms=settings.vad_pause_ms),
        asr=asr,
        router=router,
        talker=talker,
        speaker=speaker,
        tasks=HttpTasks(settings.tasks_url, delegations=delegations),
        turn=turn,
        backend=backend,
    )


app = create_app(settings, ports_factory, delegations=delegations)
