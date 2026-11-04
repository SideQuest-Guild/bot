"""A small client for any OpenAI-compatible chat completions API with tool calling.

OpenRouter, OpenAI, Google's OpenAI-compatible Gemini endpoint, Groq and a local Ollama all speak this shape, so the
provider and model are just settings.

Reasoning models (Claude Haiku 5.5, GPT-6 Luna) think by default and bill thinking as output. ``reasoning`` sets
OpenRouter's ``reasoning.effort`` for every request. When a model does think, its ``reasoning_details`` are kept on
the assistant message so they go back with the tool results, which Claude needs to carry on after a tool call.
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Final

import httpx
from pydantic import BaseModel, Field, ValidationError

RATE_LIMIT_RETRIES: Final = 3
MAX_RETRY_WAIT_SECONDS: Final = 20.0


def _retry_after(response: httpx.Response) -> float:
    """Seconds to wait after a 429: ``Retry-After`` if the API sent one, capped, else a few seconds."""
    try:
        seconds = float(response.headers.get("retry-after", "5"))
    except ValueError:
        seconds = 5.0
    return min(max(seconds, 1.0), MAX_RETRY_WAIT_SECONDS)


class LLMError(Exception):
    """The model API failed or answered with something that isn't a chat completion."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool call the model asked for. ``arguments`` is None when the model sent JSON that doesn't parse."""

    id: str
    name: str
    arguments: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class Usage:
    """Tokens a request used, and what it cost in USD when the API says (OpenRouter does)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None

    def __add__(self, other: Usage) -> Usage:
        cost = None if self.cost is None and other.cost is None else (self.cost or 0.0) + (other.cost or 0.0)
        return Usage(self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens, cost)


@dataclass(frozen=True, slots=True)
class Reply:
    """One model turn: ``message`` goes back into the history verbatim; ``text`` and ``tool_calls`` are parsed."""

    message: dict[str, Any]
    text: str
    tool_calls: list[ToolCall]
    usage: Usage = Usage()


class _Function(BaseModel):
    name: str
    arguments: str = ""


class _ToolCall(BaseModel):
    id: str
    function: _Function


class _Message(BaseModel):
    content: str | None = None
    tool_calls: list[_ToolCall] | None = None
    reasoning_details: list[dict[str, Any]] | None = None


class _Choice(BaseModel):
    message: _Message


class _Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = None


class _Completion(BaseModel):
    choices: list[_Choice] = Field(min_length=1)
    usage: _Usage | None = None


def _parse_arguments(raw: str) -> dict[str, Any] | None:
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class ChatClient:
    """``POST {base_url}/chat/completions`` with tools. Pass ``http`` to reuse a client (tests pass a mocked one)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        reasoning: str | None = None,
        max_tokens: int = 4000,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._reasoning = reasoning
        self._max_tokens = max_tokens
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=60.0)

    async def aclose(self) -> None:
        """Close the HTTP client if this instance created it."""
        if self._owns_http:
            await self._http.aclose()

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        """POST ``body``, waiting and retrying when rate limited (429), up to ``RATE_LIMIT_RETRIES`` times."""
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                response = await self._http.post(self._url, json=body, headers=self._headers)
            except httpx.HTTPError as exc:
                msg = f"model API unreachable: {exc}"
                raise LLMError(msg) from exc
            if response.status_code != httpx.codes.TOO_MANY_REQUESTS or attempt == RATE_LIMIT_RETRIES:
                return response
            await asyncio.sleep(_retry_after(response))
        return response

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> Reply:
        """One model turn over ``messages``, offering ``tools`` if any."""
        body: dict[str, Any] = {"model": self.model, "messages": messages, "max_tokens": self._max_tokens}
        if tools:
            body["tools"] = tools
        if self._reasoning:
            body["reasoning"] = {"effort": self._reasoning}
        response = await self._post(body)
        if response.is_error:
            msg = f"model API answered {response.status_code}: {response.text[:300]}"
            raise LLMError(msg)
        try:
            completion = _Completion.model_validate_json(response.content)
        except ValidationError as exc:
            msg = "model API sent something that isn't a chat completion"
            raise LLMError(msg) from exc
        message = completion.choices[0].message
        calls = message.tool_calls or []
        history_message: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
        if calls:
            history_message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.function.name, "arguments": call.function.arguments},
                }
                for call in calls
            ]
        if message.reasoning_details:
            history_message["reasoning_details"] = message.reasoning_details
        usage = completion.usage or _Usage()
        return Reply(
            message=history_message,
            text=(message.content or "").strip(),
            tool_calls=[
                ToolCall(id=call.id, name=call.function.name, arguments=_parse_arguments(call.function.arguments))
                for call in calls
            ],
            usage=Usage(usage.prompt_tokens, usage.completion_tokens, usage.cost),
        )
