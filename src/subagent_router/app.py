from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

try:
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse
except ImportError as exc:  # pragma: no cover - exercised by runtime startup.
    raise SystemExit(
        "Install proxy dependencies first: pip install -e '.[server]'"
    ) from exc

from .normalization import (
    NormalizedRequest,
    PayloadNormalizationError,
    ToolNameMapping,
    normalize_request,
    redact,
)
from .settings import Settings


SETTINGS = Settings.from_env()
MAX_REQUEST_BODY_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_SESSION_EVENTS = 200

app = FastAPI(title="Subagent Router")

RESPONSE_STATES: dict[str, list[dict[str, Any]]] = {}
REASONING_CONTENT_BY_CALL_ID: dict[str, str] = {}
REASONING_CONTENT_BY_ASSISTANT_TEXT: dict[str, str] = {}
MAX_RESPONSE_STATES = 100
SESSION_EVENTS: list[dict[str, Any]] = []
ACTIVITY_STATE: dict[str, Any] = {
    "started_at": None,
    "last_request_at": None,
    "last_response_at": None,
    "last_error_at": None,
    "request_count": 0,
    "response_count": 0,
    "error_count": 0,
    "last_trace_id": None,
    "last_model": None,
    "last_output_kind": None,
    "last_end_turn": None,
}


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

    try:
        normalized = normalize_request(
            payload,
            previous_messages=previous_messages,
            reasoning_content_by_call_id=REASONING_CONTENT_BY_CALL_ID,
            reasoning_content_by_assistant_text=REASONING_CONTENT_BY_ASSISTANT_TEXT,
            allow_apply_patch_enabled=SETTINGS.allow_apply_patch,
        )
    except PayloadNormalizationError as exc:
        record_activity("error", trace_id=trace_id)
        trace_line(trace_id, f"normalize_error status=400 message={preview_text(str(exc))}")
        return error_response(str(exc), status_code=400)

    record_activity("request", trace_id=trace_id, model=normalized.requested_model)
    trace_request(trace_id, normalized)

    try:
        chat_response = await call_deepseek(normalized)
    except httpx.HTTPStatusError as exc:
        record_activity("error", trace_id=trace_id)
        path = log_provider_error(normalized, exc.response)
        message = safe_provider_error(exc.response)
        trace_line(
            trace_id,
            f"provider_error status={exc.response.status_code} message={preview_text(message)} diagnostic={path}",
        )
        return error_response(message, status_code=exc.response.status_code)
    except httpx.HTTPError as exc:
        record_activity("error", trace_id=trace_id)
        trace_line(trace_id, f"transport_error status=502 message={preview_text(str(exc))}")
        return error_response(f"provider transport error: {exc}", status_code=502)

    response = responses_object(payload, normalized, chat_response)
    record_activity(
        "response",
        trace_id=trace_id,
        model=normalized.requested_model,
        output_kind=response_output_kind(response),
        end_turn=response.get("end_turn"),
    )
    trace_response(trace_id, response)
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
    output_kind: str | None = None,
    end_turn: bool | None = None,
) -> None:
    now = int(time.time())
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
    event = session_event_from_response(trace_id, normalized, response)
    SESSION_EVENTS.append(event)
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
            if event.get("end_turn") is True and event.get("messages")
        ),
        None,
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
    last_seen_at = max(
        timestamp
        for timestamp in (
            ACTIVITY_STATE["last_request_at"],
            ACTIVITY_STATE["last_response_at"],
            ACTIVITY_STATE["last_error_at"],
            0,
        )
        if isinstance(timestamp, int)
    )
    seconds_since_last_activity = now - last_seen_at if last_seen_at else None
    return {
        **ACTIVITY_STATE,
        "now": now,
        "last_activity_at": last_seen_at or None,
        "seconds_since_last_activity": seconds_since_last_activity,
        "active_within_120s": (
            seconds_since_last_activity is not None and seconds_since_last_activity <= 120
        ),
        "paths": SETTINGS.sanitized_paths(),
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
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/debug/activity")
async def debug_activity() -> dict[str, Any]:
    return activity_snapshot()


@app.get("/debug/paths")
async def debug_paths() -> dict[str, str]:
    return SETTINGS.sanitized_paths()


async def call_deepseek(normalized: NormalizedRequest) -> dict[str, Any]:
    if SETTINGS.mock_deepseek:
        return mock_deepseek_response(normalized)

    if not SETTINGS.deepseek_api_key:
        raise httpx.HTTPError("DEEPSEEK_API_KEY is required")

    body: dict[str, Any] = {
        "model": SETTINGS.deepseek_model or normalized.model,
        "messages": normalized.messages,
    }
    if normalized.tools:
        body["tools"] = normalized.tools
        body["tool_choice"] = "auto"
    if SETTINGS.send_parallel_tool_calls:
        body["parallel_tool_calls"] = normalized.parallel_tool_calls

    async with httpx.AsyncClient(timeout=None) as client:
        response = await client.post(
            f"{SETTINGS.deepseek_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {SETTINGS.deepseek_api_key}"},
            json=body,
        )
        response.raise_for_status()
        return response.json()


def responses_object(
    original_payload: dict[str, Any],
    normalized: NormalizedRequest,
    chat_response: dict[str, Any],
) -> dict[str, Any]:
    response_id = f"resp_{uuid.uuid4().hex}"
    output_items, assistant_messages, reasoning_by_call_id = response_items_from_chat(
        chat_response,
        normalized.tool_name_map,
    )
    has_tool_calls = any(item["type"] in {"function_call", "custom_tool_call"} for item in output_items)

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
        mapping = tool_name_map.get(chat_name, ToolNameMapping(name=chat_name))
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


def normalize_usage(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        }

    input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or input_tokens + output_tokens)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 0},
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
        "requested_model": normalized.requested_model,
        "upstream_model": SETTINGS.deepseek_model or normalized.model,
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
    return f"provider rejected request: {body}"


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
            if key_text.lower() in {"messages", "input", "prompt", "content", "text", "diff", "reasoning_content"}:
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


def mock_deepseek_response(normalized: NormalizedRequest) -> dict[str, Any]:
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
