# Codex Responses Protocol Findings

This document records the Codex Responses API shapes relevant to a local
DeepSeek compatibility proxy. It is based on source inspection, not provider
documentation.

## Request Shape

Codex builds a `Prompt` with model-visible `input`, `tools`,
`parallel_tool_calls`, base instructions, and optional output schema controls
(`codex-rs/core/src/client_common.rs:26-48`). For `wire_api = "responses"`,
Codex serializes that prompt into `ResponsesApiRequest` with:

- `model`
- `instructions`
- `input`
- `tools`
- `tool_choice: "auto"`
- `parallel_tool_calls`
- `reasoning`
- `store`
- `stream: true`
- `include`
- optional `service_tier`
- optional `prompt_cache_key`
- optional `text`
- optional `client_metadata`

The exact struct is in `codex-rs/codex-api/src/common.rs:165-185`, and the
builder is in `codex-rs/core/src/client.rs:840-891`.

HTTP Responses requests do not currently include `previous_response_id` in this
struct. The websocket path can add `previous_response_id` when it can prove a
request is an incremental continuation of the previous one
(`codex-rs/core/src/client.rs:986-1010`). The proxy MVP should still accept
`previous_response_id` because Codex-compatible clients may send it.

## Input Items

Codex input items use Responses-style tagged objects
(`codex-rs/protocol/src/models.rs:660-694`):

- `{"type":"message","role":"user|assistant|developer", "content":[...]}`
- `{"type":"function_call_output","call_id":"...", "output": ...}`
- `{"type":"mcp_tool_call_output","call_id":"...", "output": ...}`
- `{"type":"custom_tool_call_output","call_id":"...", "name":"...", "output": ...}`
- `{"type":"tool_search_output","call_id":"...", "status":"...", "execution":"client", "tools":[...]}`

Message content items are tagged as `input_text`, `input_image`, or
`output_text` (`codex-rs/protocol/src/models.rs:697-711`). Codex can send
`role = "developer"` in model context; DeepSeek chat completions accepts
`system`, `user`, `assistant`, `tool`, and provider-specific roles, but rejected
`developer` during prior trials. The proxy must map `developer` to `system`
before calling DeepSeek.

## Output Items Codex Understands

Codex parses `response.output_item.done` and `response.output_item.added` into
`ResponseItem` (`codex-rs/codex-api/src/sse/responses.rs:297-324` and
`410-416`). The relevant `ResponseItem` variants are defined in
`codex-rs/protocol/src/models.rs:741-880`:

- `message`: `role`, `content`, optional `phase`
- `function_call`: `name`, optional `namespace`, JSON-string `arguments`,
  `call_id`
- `custom_tool_call`: `name`, string `input`, `call_id`
- `local_shell_call`: `call_id` or legacy `id`, `status`, `action`
- `tool_search_call`: optional `call_id`, `execution`, `arguments`
- `web_search_call`
- `image_generation_call`
- output echoes such as `function_call_output` and `custom_tool_call_output`

The easiest MVP path is to emit `function_call` output items for DeepSeek
`tool_calls`. Codex routes `function_call` by name and preserves its `call_id`
when dispatching to the tool runtime (`codex-rs/core/src/tools/router.rs:190-205`).

## Tool Definitions Codex Sends

Codex serializes tool specs directly as Responses API tool JSON
(`codex-rs/tools/src/tool_spec.rs:18-57`):

- `{"type":"function", ...}`
- `{"type":"namespace", ...}`
- `{"type":"tool_search", "execution":"client", ...}`
- `{"type":"local_shell"}`
- `{"type":"image_generation", "output_format":"..."}`
- `{"type":"web_search", ...}`
- `{"type":"custom", ...}`

`exec_command` is a normal function tool with required `cmd` and optional
`workdir`, `shell`, `tty`, `yield_time_ms`, `max_output_tokens`, `login`, and
approval fields (`codex-rs/tools/src/local_tool.rs:19-76`). Codex also supports
legacy shell shapes: `shell`/`container.exec` expect an argument object with
`command: string[]`, while `shell_command` expects `command: string`
(`codex-rs/protocol/src/models.rs:1251-1298`).

DeepSeek rejected unsupported Responses tool types such as `image_generation`.
For the MVP proxy, only `function` tools should be forwarded to DeepSeek chat
completions. Unsupported tool types should be dropped with a sanitized warning,
or the request should fail with a structured 400 when dropping would change
required behavior.

## Streaming Requirements

Codex consumes these SSE event types from the Responses stream:

- `response.created`
- `response.output_item.added`
- `response.output_text.delta`
- `response.custom_tool_call_input.delta`
- `response.reasoning_summary_text.delta`
- `response.reasoning_text.delta`
- `response.reasoning_summary_part.added`
- `response.output_item.done`
- `response.completed`
- `response.failed`
- `response.incomplete`

The parser maps `response.completed.response.id`, optional `usage`, and optional
`end_turn` into `ResponseEvent::Completed`
(`codex-rs/codex-api/src/sse/responses.rs:132-166` and `392-407`). Codex treats
stream EOF before `response.completed` as an error
(`codex-rs/core/src/session/turn.rs:1877-1895`), then breaks the sampling loop
on `ResponseEvent::Completed` (`codex-rs/core/src/session/turn.rs:2087-2108`).

Therefore the proxy must always emit a parseable `response.completed` event for
successful streams, including streams that contain only tool calls.

## DeepSeek Compatibility Gap

Codex sends richer Responses payloads than DeepSeek chat completions accepts:

- `developer` role must become `system`.
- Responses built-in tools such as `image_generation`, `local_shell`,
  `tool_search`, `web_search`, and `custom` are not directly accepted as
  DeepSeek chat tools.
- DeepSeek chat uses assistant messages with `tool_calls`, followed by `tool`
  messages whose `tool_call_id` exactly matches. Codex instead sends/receives
  Responses items such as `function_call` and `function_call_output`.
- DeepSeek can reject a chat history where an assistant `tool_calls` message is
  not immediately followed by all matching tool outputs. The proxy must build a
  valid chat history from Codex Responses items by preserving each
  `tool_call_id`/`call_id` pair.

The proxy does not need to execute `exec_command` for the MVP. It should emit a
`function_call` item with the DeepSeek tool call id as Codex `call_id`; Codex
will execute the tool and send the result back as `function_call_output` in the
next `/v1/responses` request.
