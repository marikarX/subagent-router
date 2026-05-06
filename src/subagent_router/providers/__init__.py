from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from subagent_router.normalization import NormalizedRequest


@dataclass(frozen=True)
class ProviderCapabilities:
    context_window: int | None = None
    supports_tools: bool = True
    supports_parallel_tool_calls: bool = False
    supports_streaming: bool = False
    cost_hint: str = "unknown"
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None
    cached_input_cost_per_million: float | None = None
    supports_reasoning: bool = False
    supports_token_accounting: bool = True
    supports_usage_reporting: bool = True
    supports_cache_tokens: bool = False
    supports_reasoning_tokens: bool = False
    timeout_seconds: float | None = None
    retry_count: int = 0


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    kind: str
    base_url: str
    api_key: str | None = None
    model: str | None = None
    provider_type: str = "openai-compatible"
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    enabled: bool = True
    timeout_seconds: float | None = None
    send_parallel_tool_calls: bool = False
    model_pricing: dict[str, dict[str, float | None]] = field(default_factory=dict)
    worker_model: str | None = None
    reviewer_model: str | None = None


@dataclass(frozen=True)
class ProviderResponse:
    provider: str
    model: str
    provider_kind: str
    chat_response: dict[str, Any]
    latency_ms: int
    estimated_usage: bool = False


class Provider(Protocol):
    config: ProviderConfig

    async def chat(self, normalized: NormalizedRequest, *, model: str | None = None) -> ProviderResponse:
        ...


def provider_model(config: ProviderConfig, requested_model: str) -> str:
    return config.model or requested_model
