from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass
from typing import Any


SECRET_KEY_RE = re.compile(
    r"(authorization|api[_-]?key|token|secret|password|cookie|set-cookie|x-api-key|^env$|environment)",
    re.IGNORECASE,
)
BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
MODEL_ALIASES = {
    "deepseek-worker": "deepseek-v4-flash",
    "deepseek-reviewer": "deepseek-v4-pro",
    "deepseek-chat": "deepseek-chat",
    "deepseek-v4-flash": "deepseek-v4-flash",
    "deepseek-v4-pro": "deepseek-v4-pro",
}
UNSUPPORTED_TOOL_TYPES = {
    "custom",
    "image_generation",
    "local_shell",
    "tool_search",
    "web_search",
}
ALLOW_APPLY_PATCH_ENV = "DEEPSEEK_ALLOW_APPLY_PATCH"


class PayloadNormalizationError(ValueError):
    """Raised when a Codex Responses payload cannot form valid chat history."""


@dataclass(frozen=True)
class ToolNameMapping:
    name: str
    namespace: str | None = None


@dataclass
class NormalizedRequest:
    model: str
    requested_model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    tool_name_map: dict[str, ToolNameMapping]
    dropped_tools: list[str]
    previous_response_id: str | None
    stream: bool
    parallel_tool_calls: bool
    used_shorthand_input: bool
    input_item_count: int
    normalized_tool_names: list[str]


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text = BEARER_RE.sub("Bearer [REDACTED]", value)
        stripped = text.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.dumps(redact(json.loads(text)), ensure_ascii=True, separators=(",", ":"))
            except (TypeError, ValueError):
                return text
        return text
    return value


def normalize_request(
    payload: dict[str, Any],
    previous_messages: list[dict[str, Any]] | None = None,
    reasoning_content_by_call_id: dict[str, str] | None = None,
    reasoning_content_by_assistant_text: dict[str, str] | None = None,
    allow_apply_patch_enabled: bool | None = None,
) -> NormalizedRequest:
    request = copy.deepcopy(payload)
    requested_model = str(request.get("model") or "deepseek-chat")
    model = upstream_model_name(requested_model)
    stream = bool(request.get("stream", False))
    parallel_tool_calls = bool(request.get("parallel_tool_calls", False))

    tool_choice = request.get("tool_choice")
    if tool_choice is not None and tool_choice != "auto":
        raise PayloadNormalizationError(
            f'unsupported tool_choice: {tool_choice!r}; only None and "auto" are supported'
        )

    input_items = responses_input_items(request.get("input", []))
    used_shorthand_input = isinstance(request.get("input"), str)
    previous_response_id = request.get("previous_response_id")
    if previous_response_id is not None:
        previous_response_id = str(previous_response_id)

    tools, tool_name_map, dropped_tools = normalize_tools(
        request.get("tools") or [],
        allow_apply_patch=allow_apply_patch(request, explicit_enabled=allow_apply_patch_enabled),
    )
    messages = list(previous_messages or [])
    initial_pending_call_ids = pending_tool_call_ids(messages)

    instructions = request.get("instructions")
    if instructions and not previous_messages:
        messages.append({"role": "system", "content": str(instructions)})

    messages.extend(
        normalize_input_items(
            input_items,
            initial_pending_call_ids=initial_pending_call_ids,
            reasoning_content_by_call_id=reasoning_content_by_call_id,
            reasoning_content_by_assistant_text=reasoning_content_by_assistant_text,
        )
    )
    if requires_reasoning_content(model):
        attach_missing_reasoning_content(messages)

    return NormalizedRequest(
        model=model,
        requested_model=requested_model,
        messages=messages,
        tools=tools,
        tool_name_map=tool_name_map,
        dropped_tools=dropped_tools,
        previous_response_id=previous_response_id,
        stream=stream,
        parallel_tool_calls=parallel_tool_calls,
        used_shorthand_input=used_shorthand_input,
        input_item_count=len(input_items),
        normalized_tool_names=[tool["function"]["name"] for tool in tools],
    )


def upstream_model_name(model: str) -> str:
    return MODEL_ALIASES.get(model, model)


def requires_reasoning_content(model: str) -> bool:
    return model.startswith("deepseek-v4-")


def attach_missing_reasoning_content(messages: list[dict[str, Any]]) -> None:
    for msg in messages:
        if msg.get("role") == "assistant" and "reasoning_content" not in msg:
            msg["reasoning_content"] = (
                "Reasoning content was not retained by the local Responses proxy."
            )


def allow_apply_patch(request: dict[str, Any], *, explicit_enabled: bool | None = None) -> bool:
    if explicit_enabled is True:
        return True
    if explicit_enabled is False:
        return False
    env_value = os.environ.get(ALLOW_APPLY_PATCH_ENV)
    if env_value == "1":
        return True
    if request.get("model") in ("deepseek-worker",):
        return True
    if isinstance(request.get("metadata"), dict):
        if request["metadata"].get("allow_apply_patch") is True:
            return True
        if isinstance(request["metadata"].get("sandbox_mode"), str) and str(
            request["metadata"]["sandbox_mode"]
        ) in ("workspace-write", "danger-full-access"):
            return True
    return False


def _normalize_tool_choice(request: dict[str, Any]) -> str | None:
    tc = request.get("tool_choice")
    if not tc:
        return "auto"
    tc_str = str(tc)
    if tc_str in ("auto", "none", "required"):
        return tc_str
    return "auto"


def responses_input_items(input_value: Any) -> list[dict[str, Any]]:
    if isinstance(input_value, str):
        return [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": input_value}]}]
    if isinstance(input_value, list):
        return list(input_value)
    return []


def normalize_input_items(
    items: list[dict[str, Any]],
    initial_pending_call_ids: set[str] | None = None,
    reasoning_content_by_call_id: dict[str, str] | None = None,
    reasoning_content_by_assistant_text: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    pending_call_ids = set(initial_pending_call_ids or {})

    for item in items:
        item_type = item.get("type")
        if item_type == "message":
            if pending_call_ids:
                raise PayloadNormalizationError(
                    f"message after pending tool calls without matching tool output: {pending_call_ids}"
                )
            msg = normalize_message(item)
            if reasoning_content_by_assistant_text and msg.get("role") == "assistant":
                text = msg.get("content")
                if text and text in reasoning_content_by_assistant_text:
                    msg["reasoning_content"] = reasoning_content_by_assistant_text[text]
            messages.append(msg)
        elif item_type in ("function_call", "mcp_tool_call"):
            tool_calls = tool_call_to_chat_tool_calls(item, item_type)
            messages.append(
                chat_assistant_message(tool_calls, reasoning_content_by_call_id=reasoning_content_by_call_id)
            )
            for tc in tool_calls:
                pending_call_ids.add(tc["id"])
        elif item_type == "custom_tool_call":
            custom_tool_call = custom_tool_call_to_chat_tool_call(item)
            messages.append(chat_assistant_message([custom_tool_call]))
            pending_call_ids.add(custom_tool_call["id"])
        elif item_type == "local_shell_call":
            ls_call = local_shell_call_to_chat_tool_call(item)
            messages.append(chat_assistant_message([ls_call]))
            pending_call_ids.add(ls_call["id"])
        elif item_type in ("function_call_output", "custom_tool_call_output", "mcp_tool_call_output"):
            tool_output = normalize_tool_output(item, pending_call_ids)
            messages.append(tool_output)
        elif item_type == "tool_search_output":
            messages.append(normalize_tool_output(item, pending_call_ids))
        else:
            raise PayloadNormalizationError(f"unsupported input item type: {item_type!r}")

    return messages


def normalize_message(item: dict[str, Any]) -> dict[str, Any]:
    role = str(item.get("role") or "user")
    if role == "developer":
        role = "system"
    if role not in ("user", "assistant", "system"):
        raise PayloadNormalizationError(f"unsupported message role: {role!r}")

    content_parts = item.get("content") or []
    text_parts: list[str] = []
    for part in content_parts:
        if isinstance(part, dict):
            part_type = part.get("type")
            if part_type == "input_text":
                text_parts.append(str(part.get("text", "")))
            elif part_type == "output_text":
                text_parts.append(str(part.get("text", "")))
            elif part_type == "input_image":
                text_parts.append("[Image omitted: DeepSeek chat does not accept images]")
            elif part_type == "input_file":
                filename = part.get("filename", "file")
                text_parts.append(f"[File omitted: {filename}]")
            else:
                text_parts.append(f"[Unsupported content type: {part_type}]")
        else:
            text_parts.append(str(part))
    content = "\n".join(text_parts) if text_parts else None
    return {"role": role, "content": content}


def tool_call_to_chat_tool_calls(item: dict[str, Any], item_type: str) -> list[dict[str, Any]]:
    if item_type == "function_call":
        call_list = [item]
    else:
        call_list = item.get("calls") or [item]
    return [chat_tool_call(call, item_type) for call in call_list]


def chat_assistant_message(
    tool_calls: list[dict[str, Any]],
    reasoning_content_by_call_id: dict[str, str] | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "role": "assistant",
        "content": None,
        "tool_calls": tool_calls,
    }
    if reasoning_content_by_call_id and tool_calls:
        reasoning = reasoning_content_by_call_id.get(tool_calls[0].get("id", ""))
        if reasoning:
            msg["reasoning_content"] = reasoning
    return msg


def normalize_tool_output(item: dict[str, Any], pending_call_ids: set[str]) -> dict[str, Any]:
    call_id = str(item.get("call_id") or "")
    if not call_id:
        raise PayloadNormalizationError("tool output is missing call_id")
    if call_id not in pending_call_ids:
        raise PayloadNormalizationError(
            f"tool output call_id={call_id!r} does not match any pending tool call"
        )
    pending_call_ids.discard(call_id)
    output = item.get("output")
    text_output = tool_output_to_text(output) if output is not None else ""
    return {"role": "tool", "tool_call_id": call_id, "content": text_output}


def chat_tool_call(item: dict[str, Any], item_type: str) -> dict[str, Any]:
    call_id = str(item.get("call_id") or item.get("id") or "")
    if not call_id:
        raise PayloadNormalizationError(f"{item_type} is missing call_id")
    if item_type == "custom_tool_call":
        name = str(item.get("name") or "")
        arguments = stringify_json(item.get("input") or "")
    else:
        namespace = item.get("namespace")
        name = chat_tool_name(str(item.get("name") or ""), str(namespace) if namespace else None)
        arguments = item.get("arguments")
        if not isinstance(arguments, str):
            arguments = stringify_json(arguments or {})

    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def local_shell_call_to_chat_tool_call(item: dict[str, Any]) -> dict[str, Any]:
    call_id = str(item.get("call_id") or item.get("id") or "")
    if not call_id:
        raise PayloadNormalizationError("local_shell_call is missing call_id")
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "local_shell",
            "arguments": stringify_json(item.get("action") or {}),
        },
    }


def normalize_tools(
    tools: list[Any],
    *,
    allow_apply_patch: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, ToolNameMapping], list[str]]:
    normalized: list[dict[str, Any]] = []
    name_map: dict[str, ToolNameMapping] = {}
    dropped: list[str] = []

    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool_type = tool.get("type")
        if tool_type == "function":
            name = str(tool.get("name") or "")
            if not name:
                dropped.append("function:<missing-name>")
                continue
            if name.startswith("browser_") or (name == "apply_patch" and not allow_apply_patch):
                dropped.append(name)
                continue
            normalized.append(chat_function_tool(name, tool))
            name_map[name] = ToolNameMapping(name=name)
        elif tool_type == "namespace":
            namespace = str(tool.get("name") or "")
            for child in tool.get("tools") or []:
                if not isinstance(child, dict) or child.get("type") != "function":
                    continue
                child_name = str(child.get("name") or "")
                if not namespace or not child_name:
                    continue
                mapped_name = chat_tool_name(child_name, namespace)
                dropped.append(mapped_name)
        elif tool_type in UNSUPPORTED_TOOL_TYPES:
            dropped.append(str(tool_type))
        else:
            dropped.append(str(tool_type or "<missing-type>"))

    return normalized, name_map, dropped


def chat_function_tool(name: str, tool: dict[str, Any]) -> dict[str, Any]:
    parameters = tool.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}

    function = {
        "name": name,
        "description": str(tool.get("description") or ""),
        "parameters": parameters,
    }
    if tool.get("strict") is True and schema_is_strict_compatible(parameters):
        function["strict"] = True

    return {
        "type": "function",
        "function": function,
    }


def schema_is_strict_compatible(schema: dict[str, Any]) -> bool:
    if schema.get("type") != "object":
        return False
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return False
    required = schema.get("required")
    if required is None:
        return not properties
    if not isinstance(required, list):
        return False
    property_names = set(properties)
    required_names = {str(item) for item in required}
    if required_names != property_names:
        return False
    return all(nested_schema_is_strict_compatible(item) for item in properties.values() if isinstance(item, dict))


def nested_schema_is_strict_compatible(schema: dict[str, Any]) -> bool:
    if schema.get("type") == "object":
        return schema_is_strict_compatible(schema)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        return all(nested_schema_is_strict_compatible(item) for item in properties.values() if isinstance(item, dict))
    return True


def chat_tool_name(name: str, namespace: str | None) -> str:
    if not namespace:
        return sanitize_tool_name(name)
    return sanitize_tool_name(f"{namespace}__{name}")


def sanitize_tool_name(name: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    return sanitized[:64] or "tool"


def tool_output_to_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        if "content" in output:
            return tool_output_to_text(output["content"])
        if "content_items" in output:
            return content_items_to_text(output["content_items"])
    return stringify_json(output)


def stringify_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def content_items_to_text(items: list[Any]) -> str:
    parts: list[str] = []
    for item in items:
        if isinstance(item, dict):
            text = str(item.get("text") or "")
            if text:
                parts.append(text)
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(parts)


def pending_tool_call_ids(messages: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for msg in messages:
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if isinstance(tc, dict):
                    ids.add(str(tc.get("id", "")))
    return ids
