"""Responses-style delegated backend adapter.

Streams ``POST /v1/responses`` from any OpenAI-compatible endpoint (llama.cpp,
vLLM, LM Studio, a hosted API). Events are yielded unchanged so the session can
wrap them in ``response.event`` envelopes and forward them to the client.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

from loguru import logger

from ..ports.backend import BackendMessage, BackendRequest

StreamTransport = Callable[[str, Mapping[str, Any]], AsyncIterator[Mapping[str, Any]]]


class ResponsesBackend:
    """Run one delegated request through an OpenAI-compatible Responses API."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        timeout_s: float = 120.0,
        stream_transport: StreamTransport | None = None,
    ) -> None:
        self.url = base_url.rstrip("/") + "/responses"
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.stream_transport = stream_transport
        self.log = logger.bind(component="responses-backend")

    async def stream(self, request: BackendRequest) -> AsyncIterator[Mapping[str, Any]]:
        """Yield Responses events for one delegated turn."""
        payload = _payload(request)
        if self.stream_transport is not None:
            async for event in self.stream_transport(self.url, payload):
                yield event
            return
        async for event in self._stream(payload):
            yield event

    async def _stream(self, payload: Mapping[str, Any]) -> AsyncIterator[Mapping[str, Any]]:
        import httpx

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            async with client.stream("POST", self.url, json=payload, headers=headers) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" not in content_type:
                    body = await response.aread()
                    for event in _fallback_events(body):
                        yield event
                    return
                async for event in _sse_events(response):
                    yield event

    async def run(self, request: BackendRequest) -> str:
        """Return the concatenated text of a delegated request."""
        parts: list[str] = []
        async for event in self.stream(request):
            if event.get("type") == "response.output_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str):
                    parts.append(delta)
        return "".join(parts)


def _payload(request: BackendRequest) -> dict[str, Any]:
    messages = [
        {"role": message.role, "content": message.content}
        for message in request.history
        if isinstance(message, BackendMessage)
    ]
    messages.append({"role": "user", "content": request.transcript})
    payload: dict[str, Any] = {
        "model": request.model,
        "input": messages,
        "stream": True,
    }
    if request.instructions:
        payload["instructions"] = "\n".join(request.instructions)
    if request.tools:
        payload["tools"] = [_tool(tool) for tool in request.tools]
        payload["tool_choice"] = "auto"
    return payload


def _tool(tool: Mapping[str, Any]) -> dict[str, Any]:
    if tool.get("type") == "function":
        return dict(tool)
    return {
        "type": "function",
        "name": tool.get("name", ""),
        "description": tool.get("description", ""),
        "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
    }


async def _sse_events(response: Any) -> AsyncIterator[Mapping[str, Any]]:
    """Parse server-sent events into their JSON payloads."""
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
        if line.strip() == "" and data_lines:
            payload = "\n".join(data_lines)
            data_lines.clear()
            if payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event
    if data_lines:
        try:
            event = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            return
        if isinstance(event, dict):
            yield event


def _fallback_events(body: bytes) -> list[Mapping[str, Any]]:
    """Turn a non-streaming Responses reply into delta and completed events."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("Responses backend returned invalid JSON") from exc
    text = payload.get("output_text")
    if not isinstance(text, str):
        text = _text_from_output(payload.get("output"))
    return [
        {"type": "response.output_text.delta", "delta": text},
        {"type": "response.completed", "response": payload},
    ]


def _text_from_output(output: Any) -> str:
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "".join(parts)
