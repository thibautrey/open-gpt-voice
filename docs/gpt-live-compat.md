# GPT-Live compatibility layer

OpenGPT-Voice exposes the GPT-Live session contract locally so a GPT-Live client
can talk to a stack you own. Nothing here calls OpenAI; `gpt-live-1` is accepted
as the protocol model identifier and is mapped to your own adapters.

The layer is deliberately split in two:

1. **Protocol and transport** — identical endpoints, event names, and payload
   shapes, implemented in `live/protocol.py`, `live/session.py`, and
   `transports/webrtc.py`.
2. **Model stack** — replaceable ports (`ports/`) with OpenAI-compatible
   adapters (`adapters/`), so any local ASR, LLM, TTS, or turn detector can
   serve the session.

## Endpoints

| GPT-Live endpoint | Status | Implementation |
| --- | --- | --- |
| `POST /v1/live/sessions` (WebRTC SDP offer/answer) | implemented | `app.create_webrtc_session` + `transports/webrtc.py` |
| `WS /v1/live/sessions` (event channel) | implemented | original OpenGPT-Voice transport |
| `WS /v1/live/sessions/{id}/attach` (sideband) | implemented | `app.attach_sideband` + `LiveSession.subscribe` |
| `POST /internal/delegations/{id}/result` | implemented | `app.delegation_result` |
| `GET /health` | implemented | liveness only |

The WebRTC request body is:

```json
{
  "session": {
    "model": "gpt-live-1",
    "delegation": {"type": "responses", "responses": {"model": "qwen3-32b"}},
    "instructions": "You are a concise voice assistant.",
    "audio": {"output": {"voice": "marin"}}
  },
  "transport": {"type": "webrtc", "sdp": "<offer>"}
}
```

`audio.format` must be omitted for WebRTC: the format is negotiated in SDP. For
the WebSocket transport, `audio.format` accepts either the GPT-Live shape
`{"type": "audio/pcm", "rate": 24000}` or the legacy
`{"encoding": "pcm16le", "sample_rate": 24000, "channels": 1}`.

The response is:

```json
{
  "session": {"id": "sess_...", "model": "gpt-live-1"},
  "transport": {"type": "webrtc", "sdp": "<answer>"}
}
```

The data channel is named `oai-events`; server events are JSON objects with the
same `type` field as the WebSocket transport. Input audio arrives on the remote
audio track as 48 kHz Opus, is downsampled to 24 kHz PCM16 for the VAD/ASR
pipeline, and output PCM is upsampled to 48 kHz for the outgoing track.

## Session lifecycle

- `session.started` is emitted once the session configuration is validated and
  the ports are built. Its payload advertises the resolved model, delegation
  settings, audio format, and voice.
- `session.update` may change instructions, thinking, commentary, and voice, but
  `model`, `audio`, and `delegation` are immutable after start
  (`immutable_field_update`).
- `session.close` ends the session. Duration limits and idle timeouts close it
  with `expired`.

## Delegation

`delegation.type` selects who answers a routed task turn:

- `client` — the session emits `session.delegation.created` and waits for the
  client to post the result to `/internal/delegations/{id}/result`.
- `responses` — the server calls an OpenAI-compatible `POST /v1/responses`
  endpoint itself, relays every streamed event as `response.event`, speaks the
  `response.output_text.delta` text, and keeps the turn in history.

`delegation.responses.model` overrides `OPENGPT_VOICE_RESPONSES_MODEL` per
session. The Responses adapter is a thin SSE client; it does not implement the
server-side tool loop, so tool calls must be handled by the upstream endpoint or
by the `client` delegation mode.

## Model stack

| Stage | Port | Adapters |
| --- | --- | --- |
| VAD | `ports/vad.py` | Silero (always local) |
| ASR | `ports/asr.py` | `faster_whisper` (local) or `openai` (`/v1/audio/transcriptions`) |
| Turn detection | `ports/turn.py` | VAD silence (`vad`) or HTTP detector (`http`, e.g. DuplexJev) |
| Routing | `ports/router.py` | GLiNER, Laya, or keyword heuristic |
| Reasoning | `ports/talker.py` | LM Studio / OpenAI-compatible chat completions |
| Delegated reasoning | `ports/backend.py` | OpenAI-compatible `/v1/responses` |
| Speech output | `ports/speaker.py` | Supertonic (local) or `openai` (`/v1/audio/speech`) |

Any OpenAI-compatible server can fill the ASR, TTS, and Responses slots. With
vLLM that includes Voxtral Realtime for transcription and Voxtral TTS for speech,
but the layer is model-agnostic on purpose.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENGPT_VOICE_ASR_PROVIDER` | `faster_whisper` | `faster_whisper` or `openai` |
| `OPENGPT_VOICE_ASR_BASE_URL` | `http://127.0.0.1:8000/v1` | OpenAI-compatible ASR root |
| `OPENGPT_VOICE_ASR_MODEL` | `whisper-1` | ASR model name |
| `OPENGPT_VOICE_ASR_API_KEY` | unset | Bearer token for ASR |
| `OPENGPT_VOICE_ASR_LANGUAGE` | unset | Optional ISO language hint |
| `OPENGPT_VOICE_TTS_PROVIDER` | `supertonic` | `supertonic` or `openai` |
| `OPENGPT_VOICE_TTS_BASE_URL` | `http://127.0.0.1:8880/v1` | OpenAI-compatible TTS root |
| `OPENGPT_VOICE_TTS_MODEL` | `kokoro` | TTS model name |
| `OPENGPT_VOICE_TTS_API_KEY` | unset | Bearer token for TTS |
| `OPENGPT_VOICE_TTS_RESPONSE_FORMAT` | `wav` | `wav` or `pcm` |
| `OPENGPT_VOICE_TURN_PROVIDER` | `vad` | `vad` or `http` |
| `OPENGPT_VOICE_TURN_URL` | `http://127.0.0.1:8090/turn` | Turn detector endpoint |
| `OPENGPT_VOICE_TURN_THRESHOLD` | `0.5` | Completion probability threshold |
| `OPENGPT_VOICE_DELEGATION_MODE` | `client` | Default `delegation.type` |
| `OPENGPT_VOICE_RESPONSES_BASE_URL` | `http://127.0.0.1:1234/v1` | Responses API root |
| `OPENGPT_VOICE_RESPONSES_MODEL` | `local-model` | Default delegated model |
| `OPENGPT_VOICE_RESPONSES_API_KEY` | unset | Bearer token for Responses |
| `OPENGPT_VOICE_LLM_API_KEY` | unset | Bearer token for chat completions |
| `OPENGPT_VOICE_WEBRTC_ICE_SERVERS` | unset | JSON array of ICE servers |
| `OPENGPT_VOICE_ROUTER_PROVIDER` | `gliner` | `gliner`, `laya`, or `heuristic` |

Install the WebRTC transport with `pip install '.[webrtc]'`. Without `aiortc`,
the WebSocket transport still works and `POST /v1/live/sessions` returns `503`.

## Honest limits

- This is a **cascade**, not a duplex speech model. The server can stop playback
  as soon as VAD detects speech, which gives responsive barge-in, but the model
  does not listen while it speaks. True overlap needs a duplex model.
- Turn-taking quality depends on the configured detector. The default is
  silence-based VAD; the HTTP detector is only as good as the endpoint behind it.
- WebRTC uses aiortc's software codecs. Echo cancellation and jitter buffering
  are the browser's responsibility; the server does not do AEC.
- One process serves all sessions; there is no horizontal scaling or GPU
  scheduling yet.
- The protocol surface is implemented, but compatibility is verified against the
  project's own tests, not against OpenAI's production service.

## Verification

```bash
PYTHONPATH=src python -m pytest -q
```

`tests/test_webrtc_api.py` covers session configuration, the WebRTC endpoint,
sideband attach, and event fan-out. The full suite currently passes with 94
tests.

## Next step: Hermes plugin

The intended consumer is a Hermes plugin that exposes this endpoint to bighelp as
a live voice model, mirroring how ChatGPT's live mode is surfaced. That plugin is
not part of this repository yet; it will speak this contract over WebRTC and
reuse the same `session.start` / `session.started` / delegation semantics.
