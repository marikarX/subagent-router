from __future__ import annotations

import time
from typing import Any

import httpx

from subagent_router.normalization import NormalizedRequest
from subagent_router.providers import ProviderConfig, ProviderResponse, provider_model


class OpenAICompatibleProvider:
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    async def check_availability(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"{self.config.base_url.rstrip('/')}/models",
                    headers=auth_headers(self.config.api_key),
                )
                if response.status_code == 404:
                    # Some providers don't support /models, just check connectivity to base_url
                    response = await client.get(self.config.base_url.rstrip('/'), headers=auth_headers(self.config.api_key))
                
                # We don't necessarily need raise_for_status here because 401 is still "reachable"
                available = response.status_code < 500
                models = []
                if response.status_code == 200:
                    try:
                        data = response.json()
                        if isinstance(data, dict) and "data" in data:
                            models = [m.get("id") for m in data["data"] if isinstance(m, dict) and m.get("id")]
                    except Exception:
                        pass
                
                return {"available": available, "status_code": response.status_code, "models": models}
        except Exception as exc:
            return {"available": False, "error": str(exc)}

    async def chat(self, normalized: NormalizedRequest, *, model: str | None = None) -> ProviderResponse:
        selected_model = model or provider_model(self.config, normalized.model)
        body: dict[str, Any] = {
            "model": selected_model,
            "messages": normalized.messages,
        }
        if normalized.tools and self.config.capabilities.supports_tools:
            body["tools"] = normalized.tools
            body["tool_choice"] = "auto"
        if self.config.send_parallel_tool_calls and self.config.capabilities.supports_parallel_tool_calls:
            body["parallel_tool_calls"] = normalized.parallel_tool_calls

        started = time.monotonic()
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            response = await client.post(
                f"{self.config.base_url.rstrip('/')}/chat/completions",
                headers=auth_headers(self.config.api_key),
                json=body,
            )
            response.raise_for_status()
            chat_response = response.json()

        return ProviderResponse(
            provider=self.config.name,
            model=selected_model,
            provider_kind=self.config.kind,
            chat_response=chat_response,
            latency_ms=int((time.monotonic() - started) * 1000),
            estimated_usage=not isinstance(chat_response.get("usage"), dict),
        )


def auth_headers(api_key: str | None) -> dict[str, str]:
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}
