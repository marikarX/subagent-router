# Architecture And Behavior

This proxy exposes a Codex-compatible `POST /v1/responses` endpoint and adapts
requests to DeepSeek chat/completions.

## Responsibilities

- Accept Codex Responses-style requests.
- Normalize input messages, content items, tools, and tool outputs.
- Call DeepSeek chat/completions.
- Convert provider messages and tool calls back to Codex Responses output
  items.
- Emit Responses-compatible SSE events for streaming requests.
- Write sanitized diagnostics for payloads, provider errors, activity, and
  session mirrors.

The proxy does not execute tools. Codex executes returned tool calls and sends
matching tool outputs in the next request.

## Request Normalization

- `developer` messages become `system` messages.
- Text content items are flattened to chat message text.
- Image content is replaced with an omitted-image marker.
- Function tools are converted to chat/completions function tools.
- Unsupported built-in Responses tools are dropped:
  `image_generation`, `local_shell`, `tool_search`, `web_search`, and
  `custom`.
- Namespace/browser tools are dropped unless explicitly supported by the proxy.
- `parallel_tool_calls` is not forwarded unless explicitly enabled.
- Malformed tool-call histories fail before provider submission.

## Tool Loop

When DeepSeek asks for a tool, the proxy returns a Codex Responses
`function_call` item:

```json
{
  "type": "function_call",
  "name": "exec_command",
  "arguments": "{\"cmd\":\"pwd\"}",
  "call_id": "call_..."
}
```

Codex executes the tool and sends a matching `function_call_output`. The proxy
then reconstructs DeepSeek chat history with an assistant `tool_calls` message
followed by a `tool` message with the same `tool_call_id`.

The DeepSeek `tool_calls[].id` value is preserved as the Codex `call_id`.

## Streaming

For successful streaming responses, the proxy emits:

1. `response.created`
2. zero or more `response.output_item.done`
3. `response.completed`

When output contains tool calls, `response.end_turn` is `false` so Codex keeps
the tool loop moving.

## State And Paths

Runtime state defaults to:

```text
$XDG_STATE_HOME/subagent-router
```

or:

```text
~/.local/state/subagent-router
```

Relative log and diagnostic path overrides resolve under
`SUBAGENT_ROUTER_STATE_DIR`. Resolved paths are available through:

```shell
subagent-router paths
curl -sS http://127.0.0.1:8787/debug/activity
curl -sS http://127.0.0.1:8787/debug/paths
```

## Codex Activation Files

`subagent-router init` installs Codex integration files. Default mode writes full
delegation instructions to `SUBAGENT_ROUTER_INSTRUCTIONS.md` and references that file from the top of
`AGENTS.md` with `Follow instructions in ...`, without embedding the full instructions in `AGENTS.md`.

See [usage.md](usage.md) for mode details.

## Non-Goals

- No shell execution inside the proxy.
- No repo access inside the proxy beyond writing sanitized diagnostics.
- No OpenAI built-in image generation support.
- No DeepSeek-native streaming delta passthrough yet; the proxy may call
  DeepSeek non-streaming and synthesize Codex SSE.
- No durable persistence for `previous_response_id`; continuation cache is
  process-local.
