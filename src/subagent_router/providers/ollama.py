from __future__ import annotations

import time
from typing import Any

import httpx

from subagent_router.normalization import NormalizedRequest
from subagent_router.providers import ProviderConfig, ProviderResponse, provider_model


class OllamaProvider:
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    async def check_availability(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{self.config.base_url.rstrip('/')}/api/tags")
                response.raise_for_status()
                data = response.json()
                models = [m.get("name") for m in data.get("models", [])]
                return {"available": True, "models": models}
        except Exception as exc:
            return {"available": False, "error": str(exc)}

    async def chat(self, normalized: NormalizedRequest, *, model: str | None = None) -> ProviderResponse:
        selected_model = model or provider_model(self.config, normalized.model)
        body: dict[str, Any] = {
            "model": selected_model,
            "messages": normalized.messages,
            "stream": False,
        }
        if normalized.tools and self.config.capabilities.supports_tools:
            body["tools"] = normalized.tools

        started = time.monotonic()
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            response = await client.post(
                f"{self.config.base_url.rstrip('/')}/api/chat",
                json=body,
            )
            response.raise_for_status()
            ollama_response = response.json()

        if "error" in ollama_response:
            raise httpx.HTTPStatusError(
                f"Ollama error: {ollama_response['error']}",
                request=response.request,
                response=response,
            )

        return ProviderResponse(
            provider=self.config.name,
            model=selected_model,
            provider_kind=self.config.kind,
            chat_response=ollama_to_chat_completion(ollama_response),
            latency_ms=int((time.monotonic() - started) * 1000),
            estimated_usage=True,
        )


def ollama_to_chat_completion(response: dict[str, Any]) -> dict[str, Any]:
    message = response.get("message") if isinstance(response.get("message"), dict) else {}
    prompt_tokens = int(response.get("prompt_eval_count") or 0)
    completion_tokens = int(response.get("eval_count") or 0)
    return {
        "choices": [
            {
                "message": {
                    "role": str(message.get("role") or "assistant"),
                    "content": message.get("content") or "",
                    "tool_calls": message.get("tool_calls") or [],
                }
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
