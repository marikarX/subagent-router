from __future__ import annotations

from typing import Any

from subagent_router.normalization import NormalizedRequest
from subagent_router.providers import ProviderConfig, ProviderResponse
from subagent_router.providers.openai_compat import OpenAICompatibleProvider


class DeepSeekProvider(OpenAICompatibleProvider):
    async def chat(self, normalized: NormalizedRequest, *, model: str | None = None) -> ProviderResponse:
        return await super().chat(normalized, model=model)


def mock_deepseek_response(normalized: NormalizedRequest) -> dict[str, Any]:
    import json

    last_user = next(
        (message for message in reversed(normalized.messages) if message.get("role") == "user"),
        {"content": "ok"},
    )
    if normalized.tools and "tool" in str(last_user.get("content", "")).lower():
        tool = normalized.tools[0]["function"]
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_mock_1",
                                "type": "function",
                                "function": {
                                    "name": tool["name"],
                                    "arguments": json.dumps({"cmd": "echo mock"}, separators=(",", ":")),
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    return {
        "choices": [{"message": {"role": "assistant", "content": f"Subagent Router: {last_user['content']}"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class MockDeepSeekProvider:
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    async def check_availability(self) -> dict[str, Any]:
        return {"available": True, "models": [self.config.model or "mock-model"]}

    async def chat(self, normalized: NormalizedRequest, *, model: str | None = None) -> ProviderResponse:
        selected_model = model or self.config.model or normalized.model
        return ProviderResponse(
            provider=self.config.name,
            model=selected_model,
            provider_kind=self.config.kind,
            chat_response=mock_deepseek_response(normalized),
            latency_ms=0,
            estimated_usage=False,
        )
