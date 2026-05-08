from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, AsyncIterator, Literal

try:
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse
except ImportError as exc:  # pragma: no cover - exercised by runtime startup.
    raise SystemExit(
        "Install proxy dependencies first: pip install -e '.[server]'"
    ) from exc

from .normalization import (
    MODEL_ALIASES,
    NormalizedRequest,
    PayloadNormalizationError,
    ToolNameMapping,
    normalize_request,
    redact,
)
from .activation import normalize_profile
from .providers import Provider, ProviderConfig, ProviderResponse
from .providers.deepseek import DeepSeekProvider, MockDeepSeekProvider
from .providers.ollama import OllamaProvider
from .providers.openai_compat import OpenAICompatibleProvider
from .settings import ProviderHealthMetadata, Settings


SETTINGS = Settings.from_env()
MAX_REQUEST_BODY_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_SESSION_EVENTS = 200

app = FastAPI(title="Subagent Router")

RESPONSE_STATES: dict[str, list[dict[str, Any]]] = {}
REASONING_CONTENT_BY_CALL_ID: dict[str, str] = {}
REASONING_CONTENT_BY_ASSISTANT_TEXT: dict[str, str] = {}
MAX_RESPONSE_STATES = 100
USAGE_LOCK = threading.Lock()
SESSION_EVENTS: list[dict[str, Any]] = []
LAST_FINAL_SESSION_EVENT: dict[str, Any] | None = None


def default_activity_state() -> dict[str, Any]:
    return {
        "started_at": None,
        "last_request_at": None,
        "last_response_at": None,
        "last_error_at": None,
        "request_count": 0,
        "response_count": 0,
        "error_count": 0,
        "last_trace_id": None,
        "last_model": None,
        "last_provider": None,
        "last_output_kind": None,
        "last_end_turn": None,
        "requests_by_provider": {},
        "requests_by_model": {},
        "errors_by_provider": {},
        "local_request_count": 0,
        "cloud_request_count": 0,
        "fallback_count": 0,
        "total_latency_ms": 0,
        "average_latency_ms": 0,
        "total_cost_usd": 0.0,
        "total_tokens": 0,
    }


ACTIVITY_STATE: dict[str, Any] = default_activity_state()


@app.post("/v1/chat/completions", response_model=None)
@app.post("/v1/responses", response_model=None)
async def create_response(request: Request) -> JSONResponse | StreamingResponse:
    trace_id = uuid.uuid4().hex[:8]

    try:
        payload = await request_json_with_limit(request)
    except RequestBodyTooLarge as exc:
        record_activity("error", trace_id=trace_id)
        trace_line(trace_id, f"request_too_large size={exc.size} limit={MAX_REQUEST_BODY_SIZE}")
        return error_response(
            f"Request body too large: {exc.size} bytes exceeds {MAX_REQUEST_BODY_SIZE} byte limit",
            status_code=413,
        )
    except json.JSONDecodeError as exc:
        record_activity("error", trace_id=trace_id)
        trace_line(trace_id, f"invalid_json status=400 message={preview_text(str(exc))}")
        return error_response("Invalid JSON request body", status_code=400)

    log_payload(payload)

    previous_response_id = payload.get("previous_response_id")
    previous_messages = RESPONSE_STATES.get(str(previous_response_id)) if previous_response_id else None

    # Translate OpenAI-style 'messages' to Codex 'input' format if needed
    if "messages" in payload and "input" not in payload:
        payload["input"] = [
            {"type": "message", "role": msg.get("role", "user"), "content": [{"type": "input_text", "text": msg.get("content", "")}]}
            for msg in payload["messages"]
        ]

    route = select_route(request, payload)
    try:
        normalized = normalize_request(
            payload,
            previous_messages=previous_messages,
            reasoning_content_by_call_id=REASONING_CONTENT_BY_CALL_ID,
            reasoning_content_by_assistant_text=REASONING_CONTENT_BY_ASSISTANT_TEXT,
            allow_apply_patch_enabled=SETTINGS.apply_patch_override(),
        )
    except PayloadNormalizationError as exc:
        requested_model = requested_model_from_payload(payload)
        record_activity("error", trace_id=trace_id, model=requested_model, provider=route.provider)
        write_failed_attempt_audit(trace_id, route, str(exc), 400, model=requested_model)
        trace_line(trace_id, f"normalize_error status=400 message={preview_text(str(exc))}")
        return error_response(str(exc), status_code=400)

    record_activity("request", trace_id=trace_id, model=normalized.requested_model, provider=route.provider)
    trace_request(trace_id, normalized)

    if budget_hard_stop_for_session(float(ACTIVITY_STATE.get("total_cost_usd", 0.0)), int(ACTIVITY_STATE.get("total_tokens", 0))):
        record_activity("error", trace_id=trace_id, provider=route.provider)
        return error_response("session budget exceeded", status_code=402)
    if budget_hard_stop_for_day():
        record_activity("error", trace_id=trace_id, provider=route.provider)
        return error_response("daily budget exceeded", status_code=402)

    provider_result: ProviderResponse | None = None
    started = time.monotonic()
    try:
        if SETTINGS.dry_run or payload.get("dry_run") is True:
            provider_result = dry_run_provider_response(normalized, route)
        else:
            provider_result = await call_provider(normalized, route)
    except httpx.HTTPStatusError as exc:
        record_activity("error", trace_id=trace_id, provider=route.provider)
        path = log_provider_error(normalized, exc.response)
        message = safe_provider_error(exc.response)
        write_failed_attempt_audit(trace_id, route, message, exc.response.status_code)
        trace_line(
            trace_id,
            f"provider_error status={exc.response.status_code} message={preview_text(message)} diagnostic={path}",
        )
        return error_response(message, status_code=exc.response.status_code)
    except httpx.HTTPError as exc:
        record_activity("error", trace_id=trace_id, provider=route.provider)
        write_failed_attempt_audit(trace_id, route, str(exc), 502)
        trace_line(trace_id, f"transport_error status=502 message={preview_text(str(exc))}")
        return error_response(f"provider transport error: {exc}", status_code=502)
    except ProviderConfigurationError as exc:
        record_activity("error", trace_id=trace_id, provider=route.provider)
        write_failed_attempt_audit(trace_id, route, str(exc), 400)
        trace_line(trace_id, f"provider_config_error status=400 message={preview_text(str(exc))}")
        return error_response(str(exc), status_code=400)
    except BudgetExceededError as exc:
        record_activity("error", trace_id=trace_id, provider=route.provider)
        write_failed_attempt_audit(trace_id, route, str(exc), 402)
        return error_response(str(exc), status_code=402)

    assert provider_result is not None
    chat_response = provider_result.chat_response
    try:
        response = responses_object(payload, normalized, chat_response, provider_result=provider_result, route=route)
    except ProviderIncompleteOutputError as exc:
        retry_result = await retry_incomplete_subagent_response(
            payload,
            normalized,
            route,
            provider_result,
            str(exc),
        )
        if retry_result is not None:
            provider_result, normalized = retry_result
            chat_response = provider_result.chat_response
            try:
                response = responses_object(payload, normalized, chat_response, provider_result=provider_result, route=route)
            except ProviderIncompleteOutputError as retry_exc:
                response = subagent_continuation_response(
                    payload,
                    normalized,
                    provider_result,
                    route,
                    str(retry_exc),
                )
                if response is None:
                    record_activity("error", trace_id=trace_id, provider=provider_result.provider)
                    write_failed_attempt_audit(trace_id, route, str(retry_exc), 502)
                    trace_line(trace_id, f"incomplete_provider_output status=502 message={preview_text(str(retry_exc))}")
                    return error_response(str(retry_exc), status_code=502)
        else:
            record_activity("error", trace_id=trace_id, provider=provider_result.provider)
            write_failed_attempt_audit(trace_id, route, str(exc), 502)
            trace_line(trace_id, f"incomplete_provider_output status=502 message={preview_text(str(exc))}")
            return error_response(str(exc), status_code=502)
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    estimated_cost = estimate_cost_usd(usage, SETTINGS.providers.get(provider_result.provider), provider_result.model)
    if budget_hard_stop(estimated_cost, usage, provider_result.provider, provider_result.model):
        record_activity("error", trace_id=trace_id, provider=provider_result.provider)
        return error_response("budget exceeded for provider response", status_code=402)
    # Post-response session check: include this response's cost/tokens
    new_session_cost = round(float(ACTIVITY_STATE.get("total_cost_usd", 0.0)) + estimated_cost, 8)
    new_session_tokens = int(ACTIVITY_STATE.get("total_tokens", 0)) + int(usage.get("total_tokens") or 0)
    if budget_hard_stop_for_session(new_session_cost, new_session_tokens):
        record_activity("error", trace_id=trace_id, provider=provider_result.provider)
        return error_response("session budget exceeded", status_code=402)
    if budget_hard_stop_for_day():
        record_activity("error", trace_id=trace_id, provider=provider_result.provider)
        return error_response("daily budget exceeded", status_code=402)
    duration_ms = int((time.monotonic() - started) * 1000)
    record_activity(
        "response",
        trace_id=trace_id,
        model=normalized.requested_model,
        provider=provider_result.provider,
        provider_kind=provider_result.provider_kind,
        latency_ms=provider_result.latency_ms or duration_ms,
        cost_usd=estimated_cost,
        total_tokens=int(usage.get("total_tokens") or 0),
        output_kind=response_output_kind(response),
        end_turn=response.get("end_turn"),
    )
    trace_response(trace_id, response)
    chain = route.attempts or [
        {
            "provider": provider_result.provider,
            "model": provider_result.model,
            "status": "ok",
            "latency_ms": provider_result.latency_ms,
        }
    ]
    agent_type = agent_type_for_model(normalized.requested_model) or agent_type_for_model(normalized.model)
    write_usage_record(
        {
            "timestamp": int(time.time()),
            "trace_id": trace_id,
            "provider": provider_result.provider,
            "provider_kind": provider_result.provider_kind,
            "model": provider_result.model,
            "requested_model": normalized.requested_model,
            "agent_type": agent_type,
            "routing_policy": route.policy,
            "selection_reason": route.reason,
            "usage": usage,
            "estimated_usage": provider_result.estimated_usage,
            "estimated_cost_usd": estimated_cost,
            "latency_ms": provider_result.latency_ms,
            "fallback_chain": chain,
        }
    )
    write_audit_record(
        {
            "timestamp": int(time.time()),
            "trace_id": trace_id,
            "status": "ok",
            "provider": provider_result.provider,
            "provider_kind": provider_result.provider_kind,
            "model": provider_result.model,
            "requested_model": normalized.requested_model,
            "agent_type": agent_type,
            "routing_policy": route.policy,
            "selection_reason": route.reason,
            "latency_ms": provider_result.latency_ms,
            "total_tokens": usage.get("total_tokens", 0),
            "usage": usage,
            "estimated_cost_usd": estimated_cost,
            "fallback_chain": chain,
        }
    )
    record_session_response(trace_id, normalized, response)
    if normalized.stream:
        return StreamingResponse(
            stream_response_events(response),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )
    return JSONResponse(response)


class RequestBodyTooLarge(ValueError):
    def __init__(self, size: int) -> None:
        super().__init__(str(size))
        self.size = size


async def request_json_with_limit(request: Request) -> dict[str, Any]:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = 0
        if declared_size > MAX_REQUEST_BODY_SIZE:
            raise RequestBodyTooLarge(declared_size)

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_REQUEST_BODY_SIZE:
            raise RequestBodyTooLarge(len(body))
    payload = json.loads(bytes(body) or b"{}")
    if not isinstance(payload, dict):
        raise json.JSONDecodeError("request body must be a JSON object", bytes(body).decode("utf-8", "ignore"), 0)
    return payload


def record_activity(
    kind: str,
    trace_id: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    provider_kind: str | None = None,
    latency_ms: int | None = None,
    cost_usd: float | None = None,
    total_tokens: int | None = None,
    output_kind: str | None = None,
    end_turn: bool | None = None,
) -> None:
    now = int(time.time())
    for key, value in default_activity_state().items():
        ACTIVITY_STATE.setdefault(key, value)
    if ACTIVITY_STATE["started_at"] is None:
        ACTIVITY_STATE["started_at"] = now
    if kind == "request":
        ACTIVITY_STATE["last_request_at"] = now
        ACTIVITY_STATE["request_count"] += 1
    elif kind == "response":
        ACTIVITY_STATE["last_response_at"] = now
        ACTIVITY_STATE["response_count"] += 1
        ACTIVITY_STATE["last_output_kind"] = output_kind
        ACTIVITY_STATE["last_end_turn"] = end_turn
    elif kind == "error":
        ACTIVITY_STATE["last_error_at"] = now
        ACTIVITY_STATE["error_count"] += 1
    if trace_id is not None:
        ACTIVITY_STATE["last_trace_id"] = trace_id
    if model is not None:
        ACTIVITY_STATE["last_model"] = model
        by_model = ACTIVITY_STATE.setdefault("requests_by_model", {})
        if kind == "response":
            by_model[model] = int(by_model.get(model, 0)) + 1
    if provider is not None:
        ACTIVITY_STATE["last_provider"] = provider
        if kind == "response":
            by_provider = ACTIVITY_STATE.setdefault("requests_by_provider", {})
            by_provider[provider] = int(by_provider.get(provider, 0)) + 1
        if kind == "error":
            errors = ACTIVITY_STATE.setdefault("errors_by_provider", {})
            errors[provider] = int(errors.get(provider, 0)) + 1
    if provider_kind == "local" and kind == "response":
        ACTIVITY_STATE["local_request_count"] = int(ACTIVITY_STATE.get("local_request_count", 0)) + 1
    if provider_kind == "cloud" and kind == "response":
        ACTIVITY_STATE["cloud_request_count"] = int(ACTIVITY_STATE.get("cloud_request_count", 0)) + 1
    if latency_ms is not None and kind == "response":
        total_latency = int(ACTIVITY_STATE.get("total_latency_ms", 0)) + latency_ms
        response_count = max(int(ACTIVITY_STATE.get("response_count", 0)), 1)
        ACTIVITY_STATE["total_latency_ms"] = total_latency
        ACTIVITY_STATE["average_latency_ms"] = int(total_latency / response_count)
    if cost_usd is not None and kind == "response":
        ACTIVITY_STATE["total_cost_usd"] = round(float(ACTIVITY_STATE.get("total_cost_usd", 0.0)) + cost_usd, 8)
    if total_tokens is not None and kind == "response":
        ACTIVITY_STATE["total_tokens"] = int(ACTIVITY_STATE.get("total_tokens", 0)) + total_tokens
    write_activity_state()


def write_activity_state() -> None:
    try:
        SETTINGS.activity_file.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.activity_file.write_text(json.dumps(activity_snapshot(), indent=2, sort_keys=True))
    except OSError:
        # Activity reporting is diagnostic only; never break provider traffic.
        pass


def record_session_response(
    trace_id: str,
    normalized: NormalizedRequest,
    response: dict[str, Any],
) -> None:
    global LAST_FINAL_SESSION_EVENT
    event = session_event_from_response(trace_id, normalized, response)
    SESSION_EVENTS.append(event)
    if event.get("end_turn") is True:
        LAST_FINAL_SESSION_EVENT = event
    if len(SESSION_EVENTS) > MAX_SESSION_EVENTS:
        del SESSION_EVENTS[:-MAX_SESSION_EVENTS]
    write_session_mirror()


def write_session_mirror() -> None:
    try:
        SETTINGS.session_mirror_file.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.session_mirror_file.write_text(
            json.dumps(session_mirror_snapshot(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        # Session mirroring is diagnostic only; never break provider traffic.
        pass


def session_mirror_snapshot() -> dict[str, Any]:
    final_event = next(
        (
            event
            for event in reversed(SESSION_EVENTS)
            if event.get("end_turn") is True
        ),
        LAST_FINAL_SESSION_EVENT,
    )
    return {
        "now": int(time.time()),
        "event_count": len(SESSION_EVENTS),
        "latest": SESSION_EVENTS[-1] if SESSION_EVENTS else None,
        "final": final_event,
        "events": SESSION_EVENTS,
    }


def session_event_from_response(
    trace_id: str,
    normalized: NormalizedRequest,
    response: dict[str, Any],
) -> dict[str, Any]:
    output = response.get("output") or []
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    messages: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            text = message_text(item)
            if text:
                messages.append(text)
        elif item.get("type") in {"function_call", "custom_tool_call"}:
            tool_calls.append(
                {
                    "name": item.get("name"),
                    "call_id": item.get("call_id"),
                    "summary": trace_output_item(item),
                }
            )
    return {
        "timestamp": int(time.time()),
        "trace_id": trace_id,
        "model": normalized.requested_model,
        "end_turn": response.get("end_turn"),
        "output_kind": response_output_kind(response),
        "total_tokens": usage.get("total_tokens", 0),
        "messages": messages,
        "tool_calls": tool_calls,
        "recent_tool_outputs": recent_tool_outputs(normalized.messages),
    }


def message_text(item: dict[str, Any]) -> str:
    content = item.get("content") or []
    return " ".join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict)
    ).strip()


def activity_snapshot() -> dict[str, Any]:
    now = int(time.time())
    state = {**default_activity_state(), **ACTIVITY_STATE}
    last_seen_at = max(
        timestamp
        for timestamp in (
            state["last_request_at"],
            state["last_response_at"],
            state["last_error_at"],
            0,
        )
        if isinstance(timestamp, int)
    )
    seconds_since_last_activity = now - last_seen_at if last_seen_at else None
    return {
        **state,
        "now": now,
        "last_activity_at": last_seen_at or None,
        "seconds_since_last_activity": seconds_since_last_activity,
        "active_within_120s": (
            seconds_since_last_activity is not None and seconds_since_last_activity <= 120
        ),
        "paths": SETTINGS.sanitized_paths(),
    }


class BudgetExceededError(ValueError):
    pass


class ProviderConfigurationError(ValueError):
    pass


class ProviderIncompleteOutputError(ValueError):
    pass


class RouteSelection:
    def __init__(
        self,
        provider: str,
        model: str | None,
        fallback_providers: list[str],
        policy: str,
        reason: str,
        model_source: Literal["manual", "policy", "none"] = "none",
    ) -> None:
        self.provider = provider
        self.model = model
        self.model_source = model_source
        self.fallback_providers = fallback_providers
        self.policy = policy
        self.reason = reason
        self.attempts: list[dict[str, Any]] = []


def select_route(request: Request, payload: dict[str, Any]) -> RouteSelection:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    manual_provider = (
        request.query_params.get("provider")
        or request.headers.get("x-subagent-provider")
        or metadata.get("provider")
    )
    manual_model = request.query_params.get("model") or request.headers.get("x-subagent-model") or metadata.get("model")
    policy_name = str(
        request.query_params.get("routing_policy")
        or request.headers.get("x-subagent-routing-policy")
        or metadata.get("routing_policy")
        or metadata.get("policy")
        or ""
    )
    if manual_provider:
        return RouteSelection(
            str(manual_provider),
            str(manual_model) if manual_model else None,
            SETTINGS.fallback_providers,
            policy_name or "manual",
            "manual override",
            model_source="manual" if manual_model else "none",
        )
    if policy_name:
        policy = built_in_policy(policy_name) | SETTINGS.routing_policies.get(policy_name, {})
        provider = str(policy.get("provider") or SETTINGS.provider)
        fallback = policy.get("fallback_providers", SETTINGS.fallback_providers)
        if isinstance(fallback, str):
            fallback_providers = [fallback]
        else:
            fallback_providers = [str(item) for item in fallback]
        model = str(policy["model"]) if policy.get("model") else None
        if not model:
            p_cfg = SETTINGS.providers.get(provider)
            if p_cfg:
                if policy_name == "safe-default" and p_cfg.worker_model:
                    model = p_cfg.worker_model
                elif policy_name == "cheap-review" and p_cfg.reviewer_model:
                    model = p_cfg.reviewer_model
        provider, fallback_providers, reason = optimize_provider_order(
            provider,
            fallback_providers,
            policy,
            base_reason=f"routing policy {policy_name}",
        )
        return RouteSelection(provider, model, fallback_providers, policy_name, reason, model_source="policy" if model else "none")
    provider, fallback_providers, reason = optimize_provider_order(
        SETTINGS.provider,
        SETTINGS.fallback_providers,
        {"automatic_routing": bool(SETTINGS.provider_predictions or SETTINGS.provider_health)},
        base_reason="default provider",
    )
    return RouteSelection(provider, None, fallback_providers, "safe-default", reason)


def built_in_policy(name: str) -> dict[str, Any]:
    policies = {
        "cheap-review": {"provider": configured_provider_or_default("deepseek")},
        "local-only": {"provider": "ollama", "fallback_providers": []},
        "high-context": {"provider": configured_provider_or_default("deepseek")},
        "fast-draft": {
            "provider": configured_provider_or_default("ollama"),
            "fallback_providers": [configured_provider_or_default("deepseek")],
        },
        "safe-default": {"provider": SETTINGS.provider, "fallback_providers": SETTINGS.fallback_providers},
        "budget-capped": {"provider": SETTINGS.provider, "fallback_providers": SETTINGS.fallback_providers},
    }
    return dict(policies.get(name, {}))


def configured_provider_or_default(name: str) -> str:
    config = SETTINGS.providers.get(name)
    if config is not None and config.enabled:
        return name
    return SETTINGS.provider


def optimize_provider_order(
    provider: str,
    fallback_providers: list[str],
    policy: dict[str, Any],
    *,
    base_reason: str,
) -> tuple[str, list[str], str]:
    automatic = policy.get("automatic_routing", policy.get("optimize", False))
    strategy = str(policy.get("strategy") or policy.get("selection_strategy") or "")
    if not automatic and strategy not in {"automatic", "score", "scored"}:
        return provider, fallback_providers, base_reason

    candidates = unique_provider_order([provider, *fallback_providers])
    scored = [(provider_score(name, policy), name) for name in candidates]
    scored.sort(key=lambda item: item[0], reverse=True)
    ordered = [name for _, name in scored]
    if not ordered:
        return provider, fallback_providers, base_reason
    selected = ordered[0]
    reason = f"{base_reason}; automatic score selected {selected}"
    return selected, ordered[1:], reason


def unique_provider_order(providers: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for provider in providers:
        if provider and provider not in seen:
            seen.add(provider)
            ordered.append(provider)
    return ordered


def provider_score(provider_name: str, policy: dict[str, Any]) -> float:
    config = SETTINGS.providers.get(provider_name)
    if config is None or not config.enabled:
        return -1_000_000.0

    prediction = SETTINGS.provider_predictions.get(provider_name)
    health = SETTINGS.provider_health.get(provider_name)
    reliability = (
        prediction.reliability_score
        if prediction and prediction.reliability_score is not None
        else (health.uptime_fraction if health else 1.0)
    )
    capability = (
        prediction.capability_score
        if prediction and prediction.capability_score is not None
        else capability_score(config)
    )
    latency_ms = (
        prediction.latency_p50_ms
        if prediction and prediction.latency_p50_ms is not None
        else (health.average_latency_ms if health and health.average_latency_ms else config.capabilities.timeout_seconds)
    )
    cost_usd = prediction.budget_prediction_usd if prediction else None
    if cost_usd is None:
        cost_usd = 0.0 if config.kind == "local" else 0.01

    score = (float(reliability or 0.0) * 100.0) + (float(capability or 0.0) * 30.0)
    if latency_ms:
        score -= min(float(latency_ms), 120_000.0) / 1000.0
    score -= float(cost_usd or 0.0) * 100.0

    timeout_budget_ms = _policy_float(policy, "timeout_budget_ms")
    if timeout_budget_ms is not None and latency_ms and float(latency_ms) > timeout_budget_ms:
        score -= 100.0
    cost_target = _policy_float(policy, "cost_target_usd")
    if cost_target is not None and cost_usd is not None and float(cost_usd) > cost_target:
        score -= 100.0
    if health and health.consecutive_errors:
        score -= min(health.consecutive_errors, 10) * 20.0
    return score


def capability_score(config: ProviderConfig) -> float:
    score = 0.0
    if config.capabilities.supports_tools:
        score += 0.35
    if config.capabilities.supports_streaming:
        score += 0.15
    if config.capabilities.supports_reasoning:
        score += 0.25
    if config.capabilities.context_window and config.capabilities.context_window >= 100_000:
        score += 0.25
    return score


def _policy_float(policy: dict[str, Any], key: str) -> float | None:
    value = policy.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def call_provider(normalized: NormalizedRequest, route: RouteSelection) -> ProviderResponse:
    candidates = [route.provider, *route.fallback_providers]
    last_error: httpx.HTTPError | None = None
    for index, provider_name in enumerate(candidates):
        config = SETTINGS.providers.get(provider_name)
        if config is None or not config.enabled:
            route.attempts.append({"provider": provider_name, "status": "skipped", "error": "not configured or disabled"})
            last_error = httpx.HTTPError(f"provider {provider_name!r} is not configured or enabled")
            continue
        if SETTINGS.allowed_providers and provider_name not in SETTINGS.allowed_providers:
            route.attempts.append({"provider": provider_name, "status": "blocked", "error": "not in allowlist"})
            raise ProviderConfigurationError(f"provider {provider_name!r} is not in the configured allowlist")
        if provider_name in SETTINGS.denied_providers:
            route.attempts.append({"provider": provider_name, "status": "blocked", "error": "denylisted"})
            raise ProviderConfigurationError(f"provider {provider_name!r} is denied by configuration")
        if route.model_source == "manual":
            selected_model = route.model or role_model_for_request(config, normalized) or default_model_for_request(config, normalized)
        else:
            selected_model = role_model_for_request(config, normalized) or route.model or default_model_for_request(config, normalized)
        try:
            provider = build_provider(config)
            if budget_hard_stop_for_request(normalized, config, selected_model):
                route.attempts.append({"provider": provider_name, "model": selected_model, "status": "budget_exceeded"})
                raise BudgetExceededError("budget exceeded before provider request")
            if index > 0:
                ACTIVITY_STATE["fallback_count"] = int(ACTIVITY_STATE.get("fallback_count", 0)) + 1
            result = await provider.chat(normalized, model=selected_model)
            route.attempts.append({
                "provider": result.provider, "model": result.model, "status": "ok",
                "latency_ms": result.latency_ms,
                "error_category": None,
            })
            update_provider_health(result.provider, success=True, latency_ms=result.latency_ms)
            return result
        except BudgetExceededError:
            raise
        except ProviderConfigurationError:
            raise
        except httpx.HTTPStatusError as exc:
            exc.response.extensions["subagent_router_provider"] = provider_name
            exc.response.extensions["subagent_router_model"] = selected_model
            cat = error_category(status_code=exc.response.status_code)
            route.attempts.append({
                "provider": provider_name, "model": selected_model, "status": "error",
                "status_code": exc.response.status_code,
                "error_category": cat,
            })
            update_provider_health(provider_name, success=False)
            if exc.response.status_code < 500 or index == len(candidates) - 1:
                raise
            last_error = exc
        except httpx.HTTPError as exc:
            cat = error_category(error_message=str(exc))
            route.attempts.append({
                "provider": provider_name, "model": selected_model, "status": "error",
                "error": preview_text(str(exc), 160),
                "error_category": cat,
            })
            update_provider_health(provider_name, success=False)
            if index == len(candidates) - 1:
                raise
            last_error = exc
    raise last_error or httpx.HTTPError("no provider candidates were available")


def build_provider(config: ProviderConfig) -> Provider:
    if config.provider_type == "deepseek":
        if SETTINGS.mock_deepseek:
            return MockDeepSeekProvider(config)
        if not config.api_key:
            raise ProviderConfigurationError("DEEPSEEK_API_KEY is required")
        return DeepSeekProvider(config)
    if config.provider_type == "ollama":
        return OllamaProvider(config)
    return OpenAICompatibleProvider(config)


def default_model_for_request(config: ProviderConfig, normalized: NormalizedRequest) -> str:
    if is_subagent_model(normalized.requested_model) or is_subagent_model(normalized.model):
        return normalized.model
    return config.model or normalized.model


def role_model_for_request(config: ProviderConfig, normalized: NormalizedRequest) -> str | None:
    agent_type = agent_type_for_model(normalized.requested_model) or agent_type_for_model(normalized.model)
    if agent_type == "explorer" and config.explorer_model:
        return config.explorer_model
    if agent_type == "worker" and config.worker_model:
        return config.worker_model
    if agent_type == "reviewer" and config.reviewer_model:
        return config.reviewer_model
    return None


def dry_run_provider_response(normalized: NormalizedRequest, route: RouteSelection) -> ProviderResponse:
    provider = route.provider
    config = SETTINGS.providers.get(provider)
    model = route.model or (config.model if config else None) or normalized.model
    return ProviderResponse(
        provider=provider,
        model=model,
        provider_kind=(config.kind if config else "unknown"),
        chat_response={
            "choices": [{"message": {"role": "assistant", "content": "Subagent Router dry run: provider request skipped"}}],
            "usage": {
                "prompt_tokens": estimate_message_tokens(normalized.messages),
                "completion_tokens": 0,
                "total_tokens": estimate_message_tokens(normalized.messages),
            },
        },
        latency_ms=0,
        estimated_usage=True,
    )


def estimate_cost_usd(usage: dict[str, Any], config: ProviderConfig | None, model: str) -> float:
    if config is None or config.kind == "local":
        return 0.0
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cached_tokens = min(input_tokens, int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0))

    # Non-cached input tokens
    miss_tokens = max(0, input_tokens - cached_tokens)

    model_rates = config.model_pricing.get(model, {})
    input_val = model_rates.get("input_cost_per_million")
    input_rate = input_val if input_val is not None else config.capabilities.input_cost_per_million
    output_val = model_rates.get("output_cost_per_million")
    output_rate = output_val if output_val is not None else config.capabilities.output_cost_per_million
    cached_val = model_rates.get("cached_input_cost_per_million")
    cached_rate = cached_val if cached_val is not None else config.capabilities.cached_input_cost_per_million

    if input_rate is None and output_rate is None:
        input_rate, output_rate, default_cached = default_price_per_million(config.name, model)
        if cached_rate is None:
            cached_rate = default_cached

    # If no explicit cached rate, fallback to full input rate
    cached_rate = cached_rate if cached_rate is not None else input_rate

    cost = (miss_tokens * (input_rate or 0.0)) + (cached_tokens * (cached_rate or 0.0)) + (output_tokens * (output_rate or 0.0))
    return round(cost / 1_000_000, 8)


def default_price_per_million(provider: str, model: str) -> tuple[float | None, float | None, float | None]:
    """Default per-million-token pricing.  Updated 2026-05-05 from api.deepseek.com."""
    if provider == "deepseek":
        if "pro" in model or "reasoner" in model:
            # DeepSeek V4 Pro (75% launch promo): $0.435 in / $0.87 out / $0.003625 cache hit
            return 0.435, 0.87, 0.003625
        # DeepSeek V4 Flash: $0.14 in (cache miss) / $0.28 out / $0.0028 cache hit
        return 0.14, 0.28, 0.0028
    return None, None, None


def budget_hard_stop_for_request(normalized: NormalizedRequest, config: ProviderConfig, model: str) -> bool:
    token_limit = lowest_configured_limit(
        SETTINGS.max_tokens_per_task,
        SETTINGS.max_tokens_per_provider.get(config.name),
        SETTINGS.max_tokens_per_model.get(model),
    )
    cost_limit = lowest_configured_limit(
        SETTINGS.max_cost_per_task,
        SETTINGS.max_spend_per_task,
        SETTINGS.max_cost_per_provider.get(config.name),
        SETTINGS.max_spend_per_provider.get(config.name),
        SETTINGS.max_cost_per_model.get(model),
        SETTINGS.max_spend_per_model.get(model),
    )
    prediction = SETTINGS.provider_predictions.get(config.name)
    estimated_prompt_tokens = estimate_message_tokens(normalized.messages)
    predicted_tokens = prediction.budget_prediction_tokens if prediction else None
    predicted_cost = prediction.budget_prediction_usd if prediction else None
    token_limit_hit = token_limit is not None and max(estimated_prompt_tokens, int(predicted_tokens or 0)) > token_limit
    cost_limit_hit = cost_limit is not None and predicted_cost is not None and predicted_cost > cost_limit
    if not (token_limit_hit or cost_limit_hit):
        return False
    if SETTINGS.budget_mode == "hard-stop":
        return True
    write_audit_record(
        {
            "timestamp": int(time.time()),
            "status": "budget-warning",
            "provider": config.name,
            "model": model,
            "estimated_prompt_tokens": estimated_prompt_tokens,
            "predicted_cost_usd": predicted_cost,
            "predicted_tokens": predicted_tokens,
            "max_cost": cost_limit,
            "max_tokens": token_limit,
            "max_tokens_per_task": SETTINGS.max_tokens_per_task,
            "max_tokens_per_provider": SETTINGS.max_tokens_per_provider.get(config.name),
            "max_tokens_per_model": SETTINGS.max_tokens_per_model.get(model),
        }
    )
    return False


def budget_hard_stop(cost_usd: float, usage: dict[str, Any], provider: str, model: str) -> bool:
    token_limit = lowest_configured_limit(
        SETTINGS.max_tokens_per_task,
        SETTINGS.max_tokens_per_provider.get(provider),
        SETTINGS.max_tokens_per_model.get(model),
    )
    cost_limit = lowest_configured_limit(
        SETTINGS.max_cost_per_task,
        SETTINGS.max_spend_per_task,
        SETTINGS.max_cost_per_provider.get(provider),
        SETTINGS.max_spend_per_provider.get(provider),
        SETTINGS.max_cost_per_model.get(model),
        SETTINGS.max_spend_per_model.get(model),
    )
    token_limit_hit = token_limit is not None and int(usage.get("total_tokens") or 0) > token_limit
    cost_limit_hit = cost_limit is not None and cost_usd > cost_limit
    if not (token_limit_hit or cost_limit_hit):
        return False
    record = {
        "timestamp": int(time.time()),
        "status": "budget-warning",
        "provider": provider,
        "model": model,
        "cost_usd": cost_usd,
        "max_cost": cost_limit,
        "max_cost_per_task": SETTINGS.max_cost_per_task,
        "max_cost_per_provider": SETTINGS.max_cost_per_provider.get(provider),
        "max_cost_per_model": SETTINGS.max_cost_per_model.get(model),
        "total_tokens": usage.get("total_tokens", 0),
        "max_tokens": token_limit,
        "max_tokens_per_task": SETTINGS.max_tokens_per_task,
        "max_tokens_per_provider": SETTINGS.max_tokens_per_provider.get(provider),
        "max_tokens_per_model": SETTINGS.max_tokens_per_model.get(model),
    }
    write_audit_record(record)
    return SETTINGS.budget_mode == "hard-stop"


def budget_hard_stop_for_session(cost_usd: float, total_tokens: int) -> bool:
    """Check session-level budget limits against in-memory activity state."""
    cost_limit = lowest_configured_limit(SETTINGS.max_cost_per_session, SETTINGS.max_spend_per_session)
    token_limit = SETTINGS.max_tokens_per_session
    cost_limit_hit = cost_limit is not None and cost_usd > cost_limit
    token_limit_hit = token_limit is not None and total_tokens > token_limit
    if not (token_limit_hit or cost_limit_hit):
        return False
    record = {
        "timestamp": int(time.time()),
        "status": "budget-warning",
        "budget_level": "session",
        "type": "cost" if cost_limit_hit else "tokens",
        "session_cost_usd": cost_usd,
        "max_cost_per_session": cost_limit,
        "session_total_tokens": total_tokens,
        "max_tokens_per_session": token_limit,
    }
    write_audit_record(record)
    return SETTINGS.budget_mode == "hard-stop"


def budget_hard_stop_for_day() -> bool:
    """Check daily budget limits against persisted usage summary."""
    cost_limit = lowest_configured_limit(SETTINGS.max_cost_per_day, SETTINGS.max_spend_per_day)
    token_limit = SETTINGS.max_tokens_per_day
    if cost_limit is None and token_limit is None:
        return False
    today = datetime.date.today().isoformat()
    summary = read_usage_summary()
    daily = summary.get("daily_usage", {}).get(today, {})
    daily_cost = float(daily.get("total_cost_usd", 0.0))
    daily_tokens = int(daily.get("total_tokens", 0))
    cost_limit_hit = cost_limit is not None and daily_cost > cost_limit
    token_limit_hit = token_limit is not None and daily_tokens > token_limit
    if not (token_limit_hit or cost_limit_hit):
        return False
    record = {
        "timestamp": int(time.time()),
        "status": "budget-warning",
        "budget_level": "daily",
        "type": "cost" if cost_limit_hit else "tokens",
        "daily_cost_usd": daily_cost,
        "max_cost_per_day": cost_limit,
        "daily_total_tokens": daily_tokens,
        "max_tokens_per_day": token_limit,
    }
    write_audit_record(record)
    return SETTINGS.budget_mode == "hard-stop"


def error_category(status_code: int | None = None, error_message: str | None = None) -> str:
    """Classify an error into a category for diagnostics."""
    if status_code is not None:
        if status_code in (401, 403):
            return "auth"
        if status_code == 429:
            return "rate_limit"
        if status_code in (408, 504):
            return "timeout"
        if status_code >= 500:
            return "server_error"
        if 400 <= status_code < 500:
            return "client_error"
    if error_message:
        lower = error_message.lower()
        if "timeout" in lower or "timed out" in lower:
            return "timeout"
        if "rate" in lower or "limit" in lower or "quota" in lower or "capacity" in lower:
            return "rate_limit"
        if "auth" in lower or "key" in lower or "credential" in lower or "unauthorized" in lower or "forbidden" in lower:
            return "auth"
        if "unavailable" in lower or "down" in lower or "503" in lower:
            return "server_error"
    return "unknown"


def update_provider_health(provider_name: str, success: bool, latency_ms: int = 0) -> None:
    """Update in-memory provider health metadata after a request attempt."""
    health = SETTINGS.provider_health.get(provider_name)
    total_req = (health.total_requests if health else 0) + 1
    total_err = (health.total_errors if health else 0) + (0 if success else 1)
    consec = 0 if success else (health.consecutive_errors if health else 0) + 1
    avg_lat = health.average_latency_ms if health else 0.0
    if success:
        avg_lat = (avg_lat * (total_req - 2) + latency_ms) / max(total_req - 1, 1) if total_req > 1 else float(latency_ms)
    error_rate = total_err / max(total_req, 1)
    SETTINGS.provider_health[provider_name] = ProviderHealthMetadata(
        last_error_at=time.time() if not success else (health.last_error_at if health else None),
        last_success_at=time.time() if success else (health.last_success_at if health else None),
        consecutive_errors=consec,
        total_requests=total_req,
        total_errors=total_err,
        error_rate=error_rate,
        average_latency_ms=avg_lat,
        uptime_fraction=1.0 - error_rate,
    )


def lowest_configured_limit(*limits: float | int | None) -> float | int | None:
    configured = [limit for limit in limits if limit is not None]
    if not configured:
        return None
    return min(configured)


def estimate_message_tokens(messages: list[dict[str, Any]]) -> int:
    chars = sum(len(str(message.get("content") or "")) for message in messages)
    return max(1, int(chars / 4))


def write_audit_record(record: dict[str, Any]) -> None:
    try:
        SETTINGS.audit_log_file.parent.mkdir(parents=True, exist_ok=True)
        with SETTINGS.audit_log_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sanitize_diagnostic(record), sort_keys=True) + "\n")
    except OSError:
        pass


def write_failed_attempt_audit(
    trace_id: str,
    route: RouteSelection,
    message: str,
    status_code: int,
    *,
    model: str | None = None,
) -> None:
    """Write an audit record with enriched fallback chain diagnostics."""
    chain_diagnostics = []
    for attempt in route.attempts:
        diag = dict(attempt)
        diag["error_category"] = error_category(
            status_code=attempt.get("status_code"),
            error_message=attempt.get("error"),
        )
        provider_name = attempt.get("provider", "")
        if provider_name:
            health = SETTINGS.provider_health.get(provider_name)
            if health:
                diag["provider_health"] = {
                    "consecutive_errors": health.consecutive_errors,
                    "error_rate": health.error_rate,
                    "average_latency_ms": health.average_latency_ms,
                }
        chain_diagnostics.append(diag)
    write_audit_record(
        {
            "timestamp": int(time.time()),
            "trace_id": trace_id,
            "status": "error",
            "provider": route.provider,
            "model": model or route.model,
            "routing_policy": route.policy,
            "selection_reason": route.reason,
            "status_code": status_code,
            "message": message,
            "fallback_chain": chain_diagnostics,
            "provider_health_snapshot": {
                name: {
                    "consecutive_errors": h.consecutive_errors,
                    "error_rate": h.error_rate,
                    "average_latency_ms": h.average_latency_ms,
                }
                for name, h in SETTINGS.provider_health.items()
            },
        }
    )


def requested_model_from_payload(payload: dict[str, Any]) -> str | None:
    model = payload.get("model")
    return str(model) if model is not None else None


def write_usage_record(record: dict[str, Any]) -> None:
    with USAGE_LOCK:
        try:
            SETTINGS.usage_file.parent.mkdir(parents=True, exist_ok=True)
            current = read_usage_summary()
            token_counts = usage_token_counts(record.get("usage", {}))
            records = current.setdefault("records", [])
            if isinstance(records, list):
                records.append(record)
                del records[:-200]
            current["request_count"] = int(current.get("request_count", 0)) + 1
            current["total_tokens"] = int(current.get("total_tokens", 0)) + token_counts["total_tokens"]
            current["total_input_tokens"] = int(current.get("total_input_tokens", 0)) + token_counts["input_tokens"]
            current["total_cached_input_tokens"] = int(current.get("total_cached_input_tokens", 0)) + token_counts["cached_tokens"]
            current["total_output_tokens"] = int(current.get("total_output_tokens", 0)) + token_counts["output_tokens"]
            current["total_cost_usd"] = round(float(current.get("total_cost_usd", 0.0)) + float(record.get("estimated_cost_usd") or 0.0), 8)
            by_provider = current.setdefault("requests_by_provider", {})
            provider = str(record.get("provider") or "unknown")
            by_provider[provider] = int(by_provider.get(provider, 0)) + 1
            by_model = current.setdefault("requests_by_model", {})
            model = str(record.get("model") or "unknown")
            by_model[model] = int(by_model.get(model, 0)) + 1
            # Track daily totals
            today = datetime.date.today().isoformat()
            daily_usage = current.setdefault("daily_usage", {})
            day = daily_usage.setdefault(today, {"total_cost_usd": 0.0, "total_tokens": 0, "request_count": 0})
            day["total_cost_usd"] = round(float(day.get("total_cost_usd", 0.0)) + float(record.get("estimated_cost_usd") or 0.0), 8)
            day["total_tokens"] = int(day.get("total_tokens", 0)) + token_counts["total_tokens"]
            day["input_tokens"] = int(day.get("input_tokens", 0)) + token_counts["input_tokens"]
            day["cached_input_tokens"] = int(day.get("cached_input_tokens", 0)) + token_counts["cached_tokens"]
            day["output_tokens"] = int(day.get("output_tokens", 0)) + token_counts["output_tokens"]
            day["request_count"] = int(day.get("request_count", 0)) + 1
            tmp_path = SETTINGS.usage_file.with_suffix(SETTINGS.usage_file.suffix + ".tmp")
            tmp_path.write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")
            tmp_path.replace(SETTINGS.usage_file)
            SETTINGS.usage_jsonl_file.parent.mkdir(parents=True, exist_ok=True)
            with SETTINGS.usage_jsonl_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(sanitize_diagnostic(record), sort_keys=True) + "\n")
        except OSError:
            pass


def usage_token_counts(usage: Any) -> dict[str, int]:
    normalized = normalize_usage(usage)
    details = normalized.get("input_tokens_details") or {}
    return {
        "input_tokens": int(normalized.get("input_tokens") or 0),
        "cached_tokens": int(details.get("cached_tokens") or 0),
        "output_tokens": int(normalized.get("output_tokens") or 0),
        "total_tokens": int(normalized.get("total_tokens") or 0),
    }


def read_usage_summary() -> dict[str, Any]:
    try:
        if SETTINGS.usage_file.exists():
            data = json.loads(SETTINGS.usage_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return {
        "request_count": 0,
        "total_tokens": 0,
        "total_cost_usd": 0.0,
        "requests_by_provider": {},
        "requests_by_model": {},
        "daily_usage": {},
        "records": [],
    }


def response_output_kind(response: dict[str, Any]) -> str:
    output = response.get("output") or []
    kinds = [item.get("type") for item in output if isinstance(item, dict)]
    if any(kind in {"function_call", "custom_tool_call"} for kind in kinds):
        return "tool_call"
    if "message" in kinds:
        return "message"
    return "none"


def trace_enabled() -> bool:
    return SETTINGS.trace_enabled


def trace_line(trace_id: str, message: str) -> None:
    if trace_enabled():
        print(f"[subagent-router {trace_id}] {message}", flush=True)


def preview_text(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 15]}... [truncated]"


def trace_request(trace_id: str, normalized: NormalizedRequest) -> None:
    dropped = f" dropped={len(normalized.dropped_tools)}" if normalized.dropped_tools else ""
    recent_outputs = recent_tool_outputs(normalized.messages)
    trace_line(
        trace_id,
        (
            f"request model={normalized.requested_model}->{normalized.model} "
            f"items={normalized.input_item_count} tools={len(normalized.tools)}{dropped} "
            f"stream={normalized.stream}"
        ),
    )
    for output in recent_outputs:
        trace_line(trace_id, output)


def trace_response(trace_id: str, response: dict[str, Any]) -> None:
    output = response.get("output") or []
    parts = [trace_output_item(item) for item in output if isinstance(item, dict)]
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    total_tokens = usage.get("total_tokens", 0)
    summary = "; ".join(part for part in parts if part) or "no output"
    trace_line(
        trace_id,
        f"response end_turn={response.get('end_turn')} total_tokens={total_tokens} {summary}",
    )


def trace_output_item(item: dict[str, Any]) -> str:
    item_type = item.get("type")
    if item_type == "message":
        content = item.get("content") or []
        text = " ".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
        ).strip()
        return f"message={preview_text(text)}" if text else "message"
    if item_type in {"function_call", "custom_tool_call"}:
        arguments = format_tool_arguments(item.get("name"), item.get("arguments"))
        suffix = f" {arguments}" if arguments else ""
        return f"tool_call name={item.get('name')} call_id={item.get('call_id')}{suffix}"
    return str(item_type or "output")


def format_tool_arguments(name: Any, arguments: Any) -> str:
    if not isinstance(arguments, str) or not arguments:
        return ""
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        return f"args={preview_text(arguments, 180)}"
    if not isinstance(parsed, dict):
        return f"args={preview_text(arguments, 180)}"
    if name == "exec_command":
        return f"cmd={preview_text(parsed.get('cmd', ''), 220)}"
    if name == "write_stdin":
        return f"session_id={parsed.get('session_id')} chars={preview_text(parsed.get('chars', ''), 120)}"
    keys = ",".join(sorted(str(key) for key in parsed)[:6])
    return f"args_keys={keys}"


def recent_tool_outputs(messages: list[dict[str, Any]], limit: int = 3) -> list[str]:
    outputs: list[str] = []
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        call_id = message.get("tool_call_id")
        content = preview_text(message.get("content", ""), 260)
        outputs.append(f"tool_output call_id={call_id} output={content}")
        if len(outputs) >= limit:
            break
    return list(reversed(outputs))


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "default_provider": SETTINGS.provider,
        "config_warnings": SETTINGS.config_warnings,
        "debug_mode": SETTINGS.debug_mode,
        "providers": {
            name: {
                "type": config.provider_type,
                "kind": config.kind,
                "enabled": config.enabled,
                "base_url": config.base_url,
                "model": config.model,
            }
            for name, config in SETTINGS.providers.items()
        },
        "provider_health": {
            name: {
                "consecutive_errors": h.consecutive_errors,
                "error_rate": h.error_rate,
                "average_latency_ms": h.average_latency_ms,
                "uptime_fraction": h.uptime_fraction,
            }
            for name, h in SETTINGS.provider_health.items()
        },
    }


@app.get("/v1/config")
async def get_config() -> dict[str, Any]:
    # Build per-provider pricing info
    provider_pricing = {}
    for name, cfg in SETTINGS.providers.items():
        if not cfg.enabled:
            continue
        
        # Check model-specific pricing for the current model
        active_model = cfg.model or ""
        model_rates = cfg.model_pricing.get(active_model, {})
        inp_val = model_rates.get("input_cost_per_million")
        inp = inp_val if inp_val is not None else cfg.capabilities.input_cost_per_million
        out_val = model_rates.get("output_cost_per_million")
        out = out_val if out_val is not None else cfg.capabilities.output_cost_per_million
        cached_val = model_rates.get("cached_input_cost_per_million")
        cached = cached_val if cached_val is not None else cfg.capabilities.cached_input_cost_per_million
        
        if inp is None and out is None:
            inp, out, cached_def = default_price_per_million(name, active_model)
            if cached is None:
                cached = cached_def
                
        # Gather all model-specific overrides for this provider
        overrides = {}
        for mname, mrates in cfg.model_pricing.items():
            overrides[mname] = {
                "in": mrates.get("input_cost_per_million"),
                "out": mrates.get("output_cost_per_million"),
                "cached": mrates.get("cached_input_cost_per_million"),
            }

        provider_pricing[name] = {
            "input_cost_per_million": inp,
            "output_cost_per_million": out,
            "cached_input_cost_per_million": cached,
            "cost_hint": cfg.capabilities.cost_hint,
            "model": active_model,
            "explorer_model": cfg.explorer_model,
            "worker_model": cfg.worker_model,
            "reviewer_model": cfg.reviewer_model,
            "model_overrides": overrides,
        }
    return {
        "provider": SETTINGS.provider,
        "delegation_profile": installed_delegation_profile_for_config(),
        "fallback_providers": SETTINGS.fallback_providers,
        "budget_mode": SETTINGS.budget_mode,
        "max_cost_per_day": SETTINGS.max_cost_per_day,
        "max_cost_per_session": SETTINGS.max_cost_per_session,
        "max_cost_per_task": SETTINGS.max_cost_per_task,
        "max_tokens_per_day": SETTINGS.max_tokens_per_day,
        "max_tokens_per_session": SETTINGS.max_tokens_per_session,
        "max_tokens_per_task": SETTINGS.max_tokens_per_task,
        "max_cost_per_provider": SETTINGS.max_cost_per_provider,
        "max_tokens_per_provider": SETTINGS.max_tokens_per_provider,
        "provider_pricing": provider_pricing,
        "providers": {name: {"model": p.model, "explorer_model": p.explorer_model, "worker_model": p.worker_model, "reviewer_model": p.reviewer_model, "enabled": p.enabled} for name, p in SETTINGS.providers.items()},
        "routing_policies": SETTINGS.routing_policies,
    }


def installed_delegation_profile_for_config() -> str | None:
    root = SETTINGS.codex_home
    if root is None:
        raw_root = os.getenv("CODEX_HOME")
        if not raw_root:
            return None
        root = Path(raw_root).expanduser()
    manifest_path = root / ".subagent-router-manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        profile = manifest.get("delegation_profile")
        if isinstance(profile, str):
            return normalize_profile(profile)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return None


@app.patch("/v1/config")
async def patch_config(request: Request) -> JSONResponse:
    global SETTINGS
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    updates = {}
    new_providers = dict(SETTINGS.providers)
    new_policies = dict(SETTINGS.routing_policies)
    providers_changed = False
    policies_changed = False

    if "provider" in data:
        updates["provider"] = str(data["provider"])
    if "budget_mode" in data:
        updates["budget_mode"] = str(data["budget_mode"])
    for key in ["max_cost_per_day", "max_tokens_per_day", "max_cost_per_session", "max_tokens_per_session", "max_cost_per_task", "max_tokens_per_task"]:
        if key in data:
            updates[key] = data[key]

    if "provider_models" in data:
        for pname, model in data["provider_models"].items():
            if pname in new_providers:
                new_providers[pname] = replace(new_providers[pname], model=str(model) if model else None)
                providers_changed = True

    if "provider_pricing" in data:
        for pname, prices in data["provider_pricing"].items():
            if pname in new_providers:
                if "model" in prices:
                    model_name = prices["model"]
                    m_pricing = dict(new_providers[pname].model_pricing)
                    m_rates = dict(m_pricing.get(model_name, {}))
                    if "input_cost_per_million" in prices: m_rates["input_cost_per_million"] = prices["input_cost_per_million"]
                    if "output_cost_per_million" in prices: m_rates["output_cost_per_million"] = prices["output_cost_per_million"]
                    if "cached_input_cost_per_million" in prices: m_rates["cached_input_cost_per_million"] = prices["cached_input_cost_per_million"]
                    m_pricing[model_name] = m_rates
                    new_providers[pname] = replace(new_providers[pname], model_pricing=m_pricing)
                else:
                    cap = new_providers[pname].capabilities
                    new_cap = replace(
                        cap,
                        input_cost_per_million=prices.get("input_cost_per_million", cap.input_cost_per_million),
                        output_cost_per_million=prices.get("output_cost_per_million", cap.output_cost_per_million),
                        cached_input_cost_per_million=prices.get("cached_input_cost_per_million", cap.cached_input_cost_per_million),
                        cost_hint="configured-api"
                    )
                    new_providers[pname] = replace(new_providers[pname], capabilities=new_cap)
                providers_changed = True

    if "provider_roles" in data:
        for pname, roles in data["provider_roles"].items():
            if pname in new_providers:
                if "explorer" in roles:
                    new_providers[pname] = replace(new_providers[pname], explorer_model=roles["explorer"])
                if "worker" in roles:
                    new_providers[pname] = replace(new_providers[pname], worker_model=roles["worker"])
                if "reviewer" in roles:
                    new_providers[pname] = replace(new_providers[pname], reviewer_model=roles["reviewer"])
                providers_changed = True

    if "routing_policies" in data:
        for policy_name, policy_updates in data["routing_policies"].items():
            existing = new_policies.get(policy_name, {})
            new_policies[policy_name] = {**existing, **policy_updates}
            policies_changed = True

    if providers_changed:
        updates["providers"] = new_providers
    if policies_changed:
        updates["routing_policies"] = new_policies

    if updates:
        SETTINGS = replace(SETTINGS, **updates).ensure_active_providers_enabled()
        SETTINGS.save_runtime_config()

        # Prepare serializable updates for response
        serializable_updates = dict(updates)
        if "providers" in serializable_updates:
            serializable_updates["providers"] = {
                name: {"model": p.model, "enabled": p.enabled}
                for name, p in serializable_updates["providers"].items()
            }

        return JSONResponse({"status": "updated", "config": serializable_updates})

    return JSONResponse({"status": "no changes"})


@app.post("/v1/reset")
async def reset_usage_stats() -> JSONResponse:
    ACTIVITY_STATE.clear()
    ACTIVITY_STATE.update(default_activity_state())
    
    # Clear tracking files
    try:
        if SETTINGS.usage_file.exists():
            SETTINGS.usage_file.write_text("{}", encoding="utf-8")
        if SETTINGS.usage_jsonl_file.exists():
            SETTINGS.usage_jsonl_file.write_text("", encoding="utf-8")
        if SETTINGS.audit_log_file.exists():
            SETTINGS.audit_log_file.write_text("", encoding="utf-8")
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
        
    return JSONResponse({"status": "reset"})


@app.get("/v1/check-provider/{name}")
async def check_provider(name: str) -> dict[str, Any]:
    config = SETTINGS.providers.get(name)
    if config is None:
        return {"available": False, "error": f"Provider {name!r} not found"}

    try:
        provider = build_provider(config)
        # Check if the provider instance has a check_availability method
        if hasattr(provider, "check_availability"):
            result = await provider.check_availability()
            return result
        else:
            return {"available": True, "note": "Live check not implemented for this provider type"}
    except Exception as exc:
        return {"available": False, "error": str(exc)}


@app.get("/debug/activity")
async def debug_activity() -> dict[str, Any]:
    return activity_snapshot()


@app.get("/debug/paths")
async def debug_paths() -> dict[str, str]:
    return SETTINGS.sanitized_paths()


@app.get("/debug/config")
async def debug_config() -> dict[str, Any]:
    return {
        "config_warnings": SETTINGS.config_warnings,
        "budget_mode": SETTINGS.budget_mode,
        "provider": SETTINGS.provider,
        "fallback_providers": SETTINGS.fallback_providers,
        "allowed_providers": SETTINGS.allowed_providers,
        "denied_providers": SETTINGS.denied_providers,
        "dry_run": SETTINGS.dry_run,
        "debug_mode": SETTINGS.debug_mode,
        "provider_predictions": {
            name: {
                "reliability_score": p.reliability_score,
                "latency_p50_ms": p.latency_p50_ms,
                "latency_p99_ms": p.latency_p99_ms,
                "capability_score": p.capability_score,
                "budget_prediction_usd": p.budget_prediction_usd,
                "budget_prediction_tokens": p.budget_prediction_tokens,
            }
            for name, p in SETTINGS.provider_predictions.items()
        },
        "provider_health": {
            name: {
                "consecutive_errors": h.consecutive_errors,
                "error_rate": h.error_rate,
                "average_latency_ms": h.average_latency_ms,
                "uptime_fraction": h.uptime_fraction,
            }
            for name, h in SETTINGS.provider_health.items()
        },
    }


async def call_deepseek(normalized: NormalizedRequest) -> dict[str, Any]:
    route = RouteSelection("deepseek", None, [], "legacy", "legacy call_deepseek")
    return (await call_provider(normalized, route)).chat_response


async def retry_incomplete_subagent_response(
    original_payload: dict[str, Any],
    normalized: NormalizedRequest,
    route: RouteSelection,
    first_result: ProviderResponse,
    reason: str,
) -> tuple[ProviderResponse, NormalizedRequest] | None:
    if not is_subagent_model(normalized.requested_model) and not is_subagent_model(normalized.model):
        return None
    metadata = original_payload.get("metadata") if isinstance(original_payload.get("metadata"), dict) else {}
    if metadata.get("disable_incomplete_retry") is True:
        return None

    retry_messages = [
        *normalized.messages,
        {
            "role": "user",
            "content": (
                "Continue from the previous tool result. Do not describe a future action. "
                "If work is complete, finish now with changed files and tests run. "
                "If work remains, call the needed tool now."
            ),
        },
    ]
    retry_normalized = replace(normalized, messages=retry_messages)
    write_audit_record(
        {
            "timestamp": int(time.time()),
            "status": "incomplete-output-retry",
            "provider": first_result.provider,
            "model": first_result.model,
            "routing_policy": route.policy,
            "selection_reason": route.reason,
            "reason": reason,
        }
    )
    return await call_provider(retry_normalized, route), retry_normalized


def subagent_continuation_response(
    original_payload: dict[str, Any],
    normalized: NormalizedRequest,
    provider_result: ProviderResponse,
    route: RouteSelection,
    reason: str,
) -> dict[str, Any] | None:
    if not is_subagent_model(normalized.requested_model) and not is_subagent_model(normalized.model):
        return None
    mapping = normalized.tool_name_map.get("exec_command")
    if mapping is None:
        return None

    response_id = f"resp_{uuid.uuid4().hex}"
    call_id = f"call_{uuid.uuid4().hex[:24]}"
    arguments = json.dumps({"cmd": "true"}, ensure_ascii=True, separators=(",", ":"))
    chat_tool_call = {
        "id": call_id,
        "type": "function",
        "function": {"name": "exec_command", "arguments": arguments},
    }
    assistant_message = {"role": "assistant", "content": None, "tool_calls": [chat_tool_call]}
    RESPONSE_STATES[response_id] = normalized.messages + [assistant_message]
    _evict_old_response_states()

    item = {
        "type": "function_call",
        "id": f"fc_0_{call_id}",
        "name": mapping.name,
        "arguments": arguments,
        "call_id": call_id,
    }
    if mapping.namespace:
        item["namespace"] = mapping.namespace
    write_audit_record(
        {
            "timestamp": int(time.time()),
            "status": "synthetic-continuation",
            "provider": provider_result.provider,
            "model": provider_result.model,
            "routing_policy": route.policy,
            "selection_reason": route.reason,
            "reason": reason,
            "tool": "exec_command",
        }
    )
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "model": original_payload.get("model") or normalized.requested_model,
        "output": [item],
        "usage": normalize_usage(None),
        "end_turn": False,
        "metadata": {
            "proxy": "subagent-router",
            "dropped_tools": normalized.dropped_tools,
            "provider": provider_result.provider,
            "provider_kind": provider_result.provider_kind,
            "provider_model": provider_result.model,
            "routing_policy": route.policy,
            "provider_selection_reason": route.reason,
            "synthetic_continuation": True,
        },
    }


def responses_object(
    original_payload: dict[str, Any],
    normalized: NormalizedRequest,
    chat_response: dict[str, Any],
    provider_result: ProviderResponse | None = None,
    route: RouteSelection | None = None,
) -> dict[str, Any]:
    response_id = f"resp_{uuid.uuid4().hex}"
    output_items, assistant_messages, reasoning_by_call_id = response_items_from_chat(
        chat_response,
        normalized.tool_name_map,
    )
    has_tool_calls = any(item["type"] in {"function_call", "custom_tool_call"} for item in output_items)
    message_texts = [
        message_text(item)
        for item in output_items
        if isinstance(item, dict) and item.get("type") == "message"
    ]
    nonempty_message_texts = [text for text in message_texts if text]

    if not output_items:
        raise ProviderIncompleteOutputError("provider returned empty output: no assistant message or tool call")
    if not has_tool_calls and not nonempty_message_texts:
        raise ProviderIncompleteOutputError("provider returned an empty final assistant message")
    if (
        not has_tool_calls
        and (is_subagent_model(normalized.requested_model) or is_subagent_model(normalized.model))
        and nonempty_message_texts
        and all(is_progress_only_final_text(text) for text in nonempty_message_texts)
    ):
        raise ProviderIncompleteOutputError("provider returned progress text instead of a final subagent result")
    if (
        not has_tool_calls
        and subagent_final_lacks_write_evidence(normalized, nonempty_message_texts)
    ):
        raise ProviderIncompleteOutputError("provider returned a final subagent result before performing the requested write")

    RESPONSE_STATES[response_id] = normalized.messages + assistant_messages
    REASONING_CONTENT_BY_CALL_ID.update(reasoning_by_call_id)
    REASONING_CONTENT_BY_ASSISTANT_TEXT.update(reasoning_by_assistant_text(assistant_messages))
    _evict_old_response_states()

    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "model": original_payload.get("model") or normalized.requested_model,
        "output": output_items,
        "usage": normalize_usage(chat_response.get("usage")),
        "end_turn": not has_tool_calls,
        "metadata": {
            "proxy": "subagent-router",
            "dropped_tools": normalized.dropped_tools,
            "provider": provider_result.provider if provider_result else "deepseek",
            "provider_kind": provider_result.provider_kind if provider_result else "cloud",
            "provider_model": provider_result.model if provider_result else normalized.model,
            "routing_policy": route.policy if route else "legacy",
            "provider_selection_reason": route.reason if route else "legacy",
        },
    }


def _evict_old_response_states() -> None:
    """Evict oldest response states over MAX_RESPONSE_STATES and prune unreferenced reasoning entries."""
    while len(RESPONSE_STATES) > MAX_RESPONSE_STATES:
        oldest_id = next(iter(RESPONSE_STATES))
        del RESPONSE_STATES[oldest_id]

    referenced_call_ids: set[str] = set()
    referenced_texts: set[str] = set()
    for msgs in RESPONSE_STATES.values():
        for msg in msgs:
            content = msg.get("content")
            if isinstance(content, str) and content:
                referenced_texts.add(content)
            for tc in msg.get("tool_calls") or []:
                call_id = tc.get("id")
                if call_id:
                    referenced_call_ids.add(call_id)

    for key in list(REASONING_CONTENT_BY_CALL_ID):
        if key not in referenced_call_ids:
            del REASONING_CONTENT_BY_CALL_ID[key]
    for key in list(REASONING_CONTENT_BY_ASSISTANT_TEXT):
        if key not in referenced_texts:
            del REASONING_CONTENT_BY_ASSISTANT_TEXT[key]


def reasoning_by_assistant_text(messages: list[dict[str, Any]]) -> dict[str, str]:
    reasoning: dict[str, str] = {}
    for message in messages:
        content = message.get("content")
        reasoning_content = message.get("reasoning_content")
        if isinstance(content, str) and content and isinstance(reasoning_content, str) and reasoning_content:
            reasoning[content] = reasoning_content
    return reasoning


def response_items_from_chat(
    chat_response: dict[str, Any],
    tool_name_map: dict[str, ToolNameMapping],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    choice = (chat_response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    output: list[dict[str, Any]] = []
    assistant_messages: list[dict[str, Any]] = []
    reasoning_by_call_id: dict[str, str] = {}

    content = message.get("content")
    if content:
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": str(content)}],
            }
        )

    tool_calls = message.get("tool_calls") or []
    reasoning_content = message.get("reasoning_content")
    if tool_calls:
        assistant_message = {"role": "assistant", "content": content if content else None, "tool_calls": tool_calls}
        if isinstance(reasoning_content, str) and reasoning_content:
            assistant_message["reasoning_content"] = reasoning_content
        assistant_messages.append(assistant_message)
    elif content:
        assistant_message = {"role": "assistant", "content": str(content)}
        if isinstance(reasoning_content, str) and reasoning_content:
            assistant_message["reasoning_content"] = reasoning_content
        assistant_messages.append(assistant_message)

    for index, call in enumerate(tool_calls):
        function = call.get("function") or {}
        chat_name = str(function.get("name") or "")
        mapping = tool_name_map.get(chat_name)
        if mapping is None:
            continue
        call_id = str(call.get("id") or f"call_{uuid.uuid4().hex}")
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {}, ensure_ascii=True, separators=(",", ":"))

        item = {
            "type": "function_call",
            "id": f"fc_{index}_{call_id}",
            "name": mapping.name,
            "arguments": arguments,
            "call_id": call_id,
        }
        if mapping.namespace:
            item["namespace"] = mapping.namespace
        output.append(item)
        if isinstance(reasoning_content, str) and reasoning_content:
            reasoning_by_call_id[call_id] = reasoning_content

    return output, assistant_messages, reasoning_by_call_id


_SUBAGENT_MODELS = {
    k
    for k in MODEL_ALIASES
    if k not in ("deepseek-chat", "deepseek-v4-flash", "deepseek-v4-pro")
}


def is_subagent_model(model: str) -> bool:
    return model in _SUBAGENT_MODELS or model.startswith("subagent-")


def agent_type_for_model(model: str | None) -> str | None:
    if not model:
        return None
    for agent_type in ("explorer", "worker", "reviewer"):
        if model in (f"subagent-router-{agent_type}", f"deepseek-{agent_type}"):
            return agent_type
    return None


def subagent_final_lacks_write_evidence(
    normalized: NormalizedRequest,
    final_texts: list[str],
) -> bool:
    if not is_subagent_model(normalized.requested_model) and not is_subagent_model(normalized.model):
        return False
    if "apply_patch" not in normalized.normalized_tool_names:
        return False
    if has_tool_call(normalized.messages, "apply_patch"):
        return False
    if not request_has_write_intent(normalized.messages):
        return False
    final_text = " ".join(final_texts)
    if final_has_no_change_rationale(final_text):
        return False
    return True


def has_tool_call(messages: list[dict[str, Any]], tool_name: str) -> bool:
    for message in messages:
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            if function.get("name") == tool_name:
                return True
    return False


def request_has_write_intent(messages: list[dict[str, Any]]) -> bool:
    user_text = " ".join(
        str(message.get("content") or "")
        for message in messages
        if message.get("role") == "user"
    ).lower()
    write_terms = (
        " make ",
        " change ",
        " update ",
        " edit ",
        " add ",
        " remove ",
        " delete ",
        " replace ",
        " patch ",
        " fix ",
        " implement ",
        " modify ",
        " rewrite ",
        " refactor ",
        " create ",
    )
    padded = f" {user_text} "
    return any(term in padded for term in write_terms)


def final_has_no_change_rationale(text: str) -> bool:
    lower = " ".join(text.strip().split()).lower()
    no_change_terms = (
        "no changes needed",
        "no change needed",
        "no changes were needed",
        "no changes required",
        "no edit needed",
        "already present",
        "already up to date",
        "already matches",
        "nothing to change",
    )
    return any(term in lower for term in no_change_terms)


def is_progress_only_final_text(text: str) -> bool:
    normalized = " ".join(text.strip().split())
    if not normalized or len(normalized) > 360:
        return False
    lower = normalized.rstrip(".:;!").lower()
    completion_terms = (
        "changed",
        "updated",
        "fixed",
        "implemented",
        "added",
        "wrote",
        "created",
        "removed",
        "renamed",
        "ran ",
        "tested",
        "verified",
        "completed",
        "finished",
        "done",
        "no changes",
    )
    if any(term in lower for term in completion_terms):
        return False
    action = r"(fix|apply|implement|inspect|check|update|run|read|look|make|change|add|write|create|review|test|patch|edit|adjust|modify|replace)"
    gerund = r"(fixing|applying|implementing|inspecting|checking|updating|running|reading|looking|making|changing|adding|writing|creating|reviewing|testing|patching|editing|adjusting|modifying|replacing)"
    patterns = (
        rf"^(first,\s+)?(now\s+)?(i'll|i will)\s+(now\s+)?{action}\b.*",
        rf"^(first,\s+)?(now\s+)?let me\s+(now\s+)?{action}\b.*",
        rf"^(first,\s+)?(now\s+)?let's\s+{action}\b.*",
        rf"^(now\s+)?(i'm|i am)\s+going\s+to\s+{action}\b.*",
        rf"^(now\s+)?going\s+to\s+{action}\b.*",
        rf"^(now\s+)?{gerund}\b.*",
        rf".*\b(i'll|i will)\s+(now\s+)?{action}\b.*",
        rf".*\blet me\s+(now\s+)?{action}\b.*",
    )
    return any(re.match(pattern, lower) for pattern in patterns)


def normalize_usage(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        }

    cache_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    if not isinstance(cache_details, dict):
        cache_details = {}

    cached_tokens = int(
        usage.get("prompt_cache_hit_tokens")
        or usage.get("input_cache_hit_tokens")
        or cache_details.get("cached_tokens")
        or 0
    )
    cache_miss_tokens = int(
        usage.get("prompt_cache_miss_tokens")
        or usage.get("input_cache_miss_tokens")
        or 0
    )
    input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or cached_tokens + cache_miss_tokens)
    output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or input_tokens + output_tokens)

    output_details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    if not isinstance(output_details, dict):
        output_details = {}
    reasoning_tokens = int(output_details.get("reasoning_tokens") or 0)

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "input_tokens_details": {"cached_tokens": min(input_tokens, cached_tokens)},
        "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
    }


async def stream_response_events(response: dict[str, Any]) -> AsyncIterator[bytes]:
    yield sse("response.created", {"type": "response.created", "response": {"id": response["id"]}})
    for item in response["output"]:
        yield sse("response.output_item.done", {"type": "response.output_item.done", "item": item})
    yield sse("response.completed", {"type": "response.completed", "response": response})
    await asyncio.sleep(0)


def sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=True, separators=(',', ':'))}\n\n".encode()


def error_response(message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "message": message,
                "type": "invalid_request_error" if status_code < 500 else "proxy_error",
                "code": str(status_code),
            }
        },
        status_code=status_code,
    )


def log_payload(payload: dict[str, Any]) -> None:
    SETTINGS.log_dir.mkdir(parents=True, exist_ok=True)
    path = SETTINGS.log_dir / f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.json"
    path.write_text(json.dumps(redact(payload), ensure_ascii=True, indent=2), encoding="utf-8")


def log_provider_error(normalized: NormalizedRequest, response: httpx.Response) -> Path:
    SETTINGS.provider_error_log_dir.mkdir(parents=True, exist_ok=True)
    upstream_body = provider_body(response)
    diagnostic = {
        "timestamp": int(time.time()),
        "provider": response.extensions.get("subagent_router_provider", "unknown"),
        "requested_model": normalized.requested_model,
        "upstream_model": response.extensions.get("subagent_router_model", normalized.model),
        "upstream_status_code": response.status_code,
        "upstream_response_body": sanitize_diagnostic(upstream_body),
        "stream": normalized.stream,
        "input_item_count": normalized.input_item_count,
        "tool_count": len(normalized.tools),
        "normalized_tool_names": normalized.normalized_tool_names,
        "dropped_tool_names": normalized.dropped_tools,
        "used_shorthand_string_input": normalized.used_shorthand_input,
    }
    path = SETTINGS.provider_error_log_dir / f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.json"
    path.write_text(json.dumps(diagnostic, ensure_ascii=True, indent=2), encoding="utf-8")
    return path


def safe_provider_error(response: httpx.Response) -> str:
    body = sanitize_diagnostic(provider_body(response))
    message = provider_error_message(body)
    if message:
        if diagnostic_contains_redaction(body):
            return f"provider rejected request: {message} ([REDACTED] details omitted)"
        return f"provider rejected request: {message}"
    return f"provider rejected request: {body}"


def provider_error_message(body: Any) -> str | None:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if message:
                return str(message)
        message = body.get("message")
        if message:
            return str(message)
    return None


def diagnostic_contains_redaction(value: Any) -> bool:
    if isinstance(value, dict):
        return any(diagnostic_contains_redaction(item) for item in value.values())
    if isinstance(value, list):
        return any(diagnostic_contains_redaction(item) for item in value)
    if isinstance(value, str):
        return "[REDACTED]" in value
    return False


def provider_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def sanitize_diagnostic(value: Any) -> Any:
    return truncate_large_strings(redact(value))


def truncate_large_strings(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in {"messages", "input", "prompt", "content", "text", "diff", "reasoning_content", "tool_calls", "arguments"}:
                sanitized[key_text] = "[OMITTED]"
            else:
                sanitized[key_text] = truncate_large_strings(item)
        return sanitized
    if isinstance(value, list):
        return [truncate_large_strings(item) for item in value]
    if isinstance(value, str):
        max_length = 4000
        if len(value) > max_length:
            return f"{value[:max_length]}...[truncated {len(value) - max_length} chars]"
        return value
    return value
