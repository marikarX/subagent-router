# Subagent Router Test Matrix

## Unit Tests

- Payload normalization maps `developer` messages to `system`.
- Content normalization preserves `input_text` and `output_text`.
- Content normalization replaces `input_image` with an omitted-image marker.
- Tool normalization converts Responses `function` tools to chat function tools.
- Tool normalization drops `image_generation` without provider submission.
- Tool normalization drops other unsupported built-ins for MVP.
- Namespace tools are flattened for DeepSeek and remain mappable back to Codex.
- `function_call` plus `function_call_output` preserves `call_id`.
- `custom_tool_call_output` becomes a chat `tool` message.
- Malformed tool-call history fails before provider submission.
- Redaction removes authorization, API keys, tokens, cookies, passwords, and
  bearer strings from logs.

## Integration Tests

- Simple text request:
  - POST `/v1/responses` with one user message.
  - Expect a Responses object or SSE stream with assistant message output.
  - Expect `response.completed` for streaming mode.

- Synthetic developer-role request:
  - Include `role = developer`.
  - Verify provider receives `system`.
  - Verify request succeeds.

- Unsupported tool request:
  - Include `image_generation` plus one normal function tool.
  - Verify DeepSeek receives only the function tool.
  - Verify response metadata records the dropped tool.

- One `exec_command` tool call:
  - Mock DeepSeek response with one `tool_calls` item named `exec_command`.
  - Verify proxy emits `function_call` with matching `call_id`.
  - Verify `response.completed.end_turn = false`.

- Multi-turn tool output continuation:
  - First request returns an `exec_command` call.
  - Second request includes the previous `function_call` and matching
    `function_call_output`.
  - Verify DeepSeek receives assistant `tool_calls` followed by a matching
    `tool` message.

- `previous_response_id` continuation:
  - First response stores assistant tool-call state.
  - Second request sends `previous_response_id` and only the new tool output.
  - Verify the proxy reconstructs valid chat history.

## Failure Tests

- Assistant tool call followed by a user message without tool output returns
  structured 400.
- Tool output with a mismatched `call_id` returns structured 400.
- Tool output with no `call_id` returns structured 400.
- Provider 4xx response is returned as a structured provider rejection without
  logging secrets.
- Provider transport failure returns 502.

## Live Codex Validation

Validate the proxy against a real Codex session after unit and synthetic
integration tests pass:

1. Configure Codex custom provider to point at the local proxy.
2. Spawn a low-cost subagent with repo read-only exploration.
3. Confirm Codex receives `function_call` items, executes `exec_command`, sends
   matching outputs, and receives a final assistant message followed by
   `response.completed`.
4. Repeat with a bounded worker task that can use `apply_patch`.
5. Run one `subagent-router run -- codex ...` command to verify ephemeral startup and
   shutdown.
