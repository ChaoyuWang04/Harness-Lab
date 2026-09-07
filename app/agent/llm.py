from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx2
from openai import OpenAI

from app.config import settings


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ModelTurn:
    content: str | None
    tool_calls: list[ToolInvocation]
    usage: dict[str, int] | None = None


class ChatClient(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn: ...


class OllamaClient:
    def __init__(self, *, base_url: str | None = None, model: str | None = None) -> None:
        self.model = model or settings.llm_model
        self._http_client = httpx2.Client(trust_env=False, timeout=settings.llm_timeout_seconds)
        self.client = OpenAI(
            base_url=base_url or settings.llm_base_url,
            api_key="ollama",
            http_client=self._http_client,
            max_retries=0,
        )

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            temperature=0,
        )
        message = response.choices[0].message
        calls = [
            ToolInvocation(
                id=call.id,
                name=call.function.name,
                arguments=call.function.arguments,
            )
            for call in (message.tool_calls or [])
        ]
        usage = None
        if response.usage is not None:
            usage = {
                "input": response.usage.prompt_tokens,
                "output": response.usage.completion_tokens,
                "total": response.usage.total_tokens,
            }
        return ModelTurn(content=message.content, tool_calls=calls, usage=usage)
