# Provider Compatibility

This document tracks which providers and models are supported, tested, or
planned for the Subagent Router. It is updated as provider adapters are
implemented.

## Legend

| Column | Meaning |
|---|---|
| Status | `implemented` = adapter exists in the current release |
| | `tested` = confirmed working with live provider |
| | `planned` = on the roadmap, not yet implemented |
| Provider | e.g. DeepSeek, OpenAI-compatible, Ollama |
| Model | Specific model identifier used in requests |
| Streaming | SSE streaming of partial responses |
| Tool Calls | `function_call` / `tool_calls` round-trip support |
| Reasoning | Provider-specific reasoning content support |
| Strict Schema | Structured output via `strict: true` schema |
| Usage Tokens | Provider reports token usage in response |

## Provider Adapter Status

### DeepSeek

**Status: implemented**

| Model | Streaming | Tool Calls | Reasoning | Strict Schema | Usage Tokens |
|---|---|---|---|---|---|
| `deepseek-chat` | Tested | Tested | N/A | N/A | Reported |
| `deepseek-v4-flash` | Tested | Tested | N/A | Strict-compatible | Reported |
| `deepseek-v4-pro` | Tested | Tested | Supported | Strict-compatible | Reported |

Model aliases mapped by the proxy:

| Codex Alias | Upstream Model |
|---|---|
| `deepseek-chat` | `deepseek-chat` |
| `deepseek-worker` | `deepseek-v4-flash` |
| `deepseek-reviewer` | `deepseek-v4-pro` |
| `deepseek-v4-flash` | `deepseek-v4-flash` |
| `deepseek-v4-pro` | `deepseek-v4-pro` |

Configuration:

- **API key**: `DEEPSEEK_API_KEY`
- **Base URL**: `DEEPSEEK_BASE_URL` (default `https://api.deepseek.com/v1`)
- **Model override**: `DEEPSEEK_MODEL`

Known limitations:

- Parallel tool calls must be explicitly enabled via
  `DEEPSEEK_SEND_PARALLEL_TOOL_CALLS=1`.
- `apply_patch` tool is dropped for reviewer requests unless
  `DEEPSEEK_ALLOW_APPLY_PATCH=1` is set.
- DeepSeek does not accept `developer` role; the proxy maps it to `system`.
- DeepSeek V4 Pro requires `reasoning_content` to be replayed after a tool
  call. The proxy stores this in process memory, keyed by tool call ID.
  This cache is **not durable** across proxy restarts.

### OpenAI-Compatible

**Status: implemented**

Works with providers exposing an OpenAI-compatible
`/chat/completions` endpoint, including OpenRouter, Groq, Together, and custom
self-hosted endpoints.

| Feature | Support |
|---|---|
| Streaming | Responses SSE output is supported; upstream streaming is not yet used |
| Tool Calls | Yes (where supported by upstream) |
| Strict Schema | Provider-dependent |
| Usage Tokens | Yes (where reported) |
| Model Mapping | User-specified |

Known limitations:

- Provider-specific rate limits, context windows, and pricing vary widely.
- Some OpenAI-compatible endpoints may not support all chat features (e.g.
  parallel tool calls, strict schemas, certain content types).
- Token accounting may differ between the upstream provider and proxy
  estimates.

### Ollama

**Status: implemented**

| Feature | Support |
|---|---|
| Streaming | Responses SSE output is supported; upstream streaming is disabled |
| Tool Calls | Model-dependent (llama3.1+ supports function calling) |
| Strict Schema | Limited |
| Usage Tokens | Uses Ollama prompt/eval counts when returned |
| API Cost | Zero (local execution) |

Known limitations:

- Tool-call fidelity varies significantly by local model. Smaller models may
  produce malformed tool calls.
- Context window depends on the loaded model.
- Local endpoint availability depends on the user's Ollama setup.

## Model Compatibility Notes

### Tool Call Support by Provider

| Provider | Tool Calls | Parallel Tool Calls | Strict |
|---|---|---|---|
| DeepSeek chat | Yes | Conditional (`SEND_PARALLEL_TOOL_CALLS`) | N/A |
| DeepSeek V4 | Yes | Yes | Yes (strict-compatible schemas) |
| OpenAI GPT-4o | Yes | Yes | Yes |
| Anthropic Claude | Planned adapter | Yes | N/A |
| Ollama (llama3.1+) | Model-dependent | Model-dependent | N/A |

### Context Windows

| Provider / Model | Context Window (estimated) |
|---|---|
| DeepSeek chat | 128K tokens |
| DeepSeek V4 flash | 256K tokens |
| DeepSeek V4 pro | 256K tokens |
| OpenAI GPT-4o | 128K tokens |
| Ollama (varies by model) | Model-dependent |

## Provider-Specific Configuration

Provider blocks can be configured in `~/.config/subagent-router/config.toml` or
project-local `.subagent-router.toml`. Environment variables exist for the
built-in DeepSeek, OpenAI-compatible, and Ollama adapters. See
[usage.md](usage.md) for the full schema and precedence rules.

## Tested Combinations (Current)

| OS | Provider | Model | Result |
|---|---|---|---|
| Linux | DeepSeek API | `deepseek-chat` | Verified |
| Linux | DeepSeek API | `deepseek-v4-flash` | Verified |
| Linux | DeepSeek API | `deepseek-v4-pro` | Verified |
| Linux | Groq (OpenAI-compatible) | `llama-3.3-70b` | Verified |
| Linux | Ollama | `qwen3.5:latest` | Verified |
| Linux | Mock (no provider) | — | Verified |

## Planned Combinations

| OS | Provider | Model | Status |
|---|---|---|---|
| macOS / Linux | Ollama | `llama3.1`, `qwen2.5`, etc. | Live validation planned |
| Any | OpenRouter | user-routed models | Live validation planned |
| Any | Groq | user-specified | Live validation planned |
| Any | Together | user-specified | Live validation planned |

## Integration Compatibility

| Primary Agent | Status | Notes |
|---|---|---|
| Codex | Tested | Full `exec_command` tool loop, streaming, multi-turn |
| Claude Code | Possible via stdio | Requires a Claude Code wrapper around `subagent-router stdio` |
| Generic CLI | Implemented | `subagent-router stdio` reads one request and writes one response |

## Integration Compatibility Limits

### Claude Code

Claude Code can be used with Subagent Router through the `subagent-router stdio`
command, but several limitations apply:

- **No native Claude Code extension**: There is no Claude Code MCP server or
  custom tool integration. All communication goes through stdin/stdout piping.
- **No tool loop**: Claude Code's tool execution loop is not proxied. The stdio
  command sends one complete request and receives one response. Multi-turn tool
  continuation is not supported in this mode.
- **No streaming**: The stdio mode uses non-streaming responses. SSE streaming
  is only available through the HTTP proxy endpoint.
- **No automatic routing**: Provider selection must be configured in advance
  via environment variables or config files. Claude Code cannot dynamically
  select providers per request.
- **Manual wrapper required**: Users must write a wrapper script that calls
  `subagent-router stdio` with the appropriate request payload and relays the
  response back to Claude Code's subprocess interface.
- **State isolation**: Each stdio invocation is stateless. The proxy does not
  maintain a session across multiple invocations from Claude Code.

### Generic CLI / Stdin/Stdout Handoff

The `subagent-router stdio` and `subagent-router handoff` commands provide
generic handoff for any CLI-based primary agent. Known limits:

- **Single-shot only**: Each invocation handles exactly one request. File-based
  handoff processes available files once (`--once`) or polls a directory.
- **No multi-turn state**: Tool-call outputs must be manually tracked by the
  caller. The handoff payload must include the full conversation history,
  including prior `function_call` and `function_call_output` messages.
- **No streaming**: The stdio/handoff path always returns a complete JSON
  response object, never an SSE stream.
- **No real-time progress**: The caller receives no intermediate status updates
  while the provider is generating a response.
- **Error isolation**: Errors are written to `*.error.json` files for file
  handoff, or to stderr for stdio handoff. The caller must monitor both paths.
- **No built-in retry**: The handoff commands do not automatically retry on
  transient provider failures. The caller should implement retry logic.

## Live Validation Gaps

The following areas lack live end-to-end validation against real provider and
primary-agent combinations. These gaps are tracked for resolution in upcoming
releases.

### Provider Live Validation

| Gap | Impact | Target |
|---|---|---|
| Token usage accuracy not verified against real provider responses | Estimated vs. reported token counts may diverge | Compare proxy estimates against provider-reported values in live tests |
| Streaming latency under load not characterized | SSE streaming is implemented but real-world latency with concurrent requests is unknown | Add benchmark with concurrent streaming requests |

### Primary-Agent Live Validation

| Gap | Impact | Target |
|---|---|---|
| No automated end-to-end test with real Codex session | Proxy behavior with real Codex tool loops, `previous_response_id`, and session recovery is only validated manually | Add scripted Codex subagent workflow that exercises tool calls and continuation |
| No live test with Claude Code | `stdio` handoff with Claude Code is theoretically possible but never validated | Create Claude Code wrapper script and validate with bounded task |
| No live test with generic CLI caller | File-based handoff is unit tested but never exercised against a real external CLI | Write reference CLI caller and validate multi-file batch processing |

### Security and Secrets Validation

| Gap | Impact | Target |
|---|---|---|
| Secret redaction not verified against real provider error payloads | Redaction regexes may miss provider-specific formats or edge cases | Add fuzz-based redaction test with real provider error samples |
| Debug bundle secrets audit not automated | Manual review is required to confirm the bundle contains no raw secrets | Add automated secrets scan in CI for generated debug bundles |
| Config file permissions not validated | The proxy does not warn when config files or state directories have world-readable permissions | Add startup permissions check for config files containing API keys |

### Cross-Platform Validation

| Gap | Impact | Target |
|---|---|---|
| No automated testing on macOS | macOS-specific path resolution, process management, and signal handling not validated | Add macOS CI runner or manual smoke test checklist |
| No testing with non-English locale | Locale-specific encoding or formatting edge cases may exist | Add locale-fuzzing to existing unit tests |
| No IPv6 validation | The proxy defaults to IPv4 loopback; IPv6 support may have edge cases | Add IPv6 smoke test and configuration documentation |

### Compatibility and Upgrade Validation

| Gap | Impact | Target |
|---|---|---|
| Upgrade path from 0.1.x to 0.2.x not validated | Config file schema, managed file manifest, and state directory layout may change | Write upgrade test that preserves user state across version bumps |
| No npm/PyPI publish dry-run | Release artifact packaging is only validated manually | Add CI step that runs `npm pack` and `pip wheel` to verify artifact contents |

## See Also


## Runtime Compatibility

| Runtime | Version | Status |
|---|---|---|
| Python | >= 3.11 | Required |
| Node.js | >= 18 | Required for npm wrapper (`bin/subagent-router.js`) |
| systemd | user services (Linux) | Optional (`subagent-router install-service`) |

## Debugging Provider Issues

When a provider behaves unexpectedly:

1. Start the proxy with trace output:
   ```shell
   DEEPSEEK_API_KEY=... subagent-router start --trace
   ```

2. Check the resolved provider error log directory:
   ```shell
   subagent-router paths
   ```

3. Inspect diagnostics (secrets redacted automatically):
   ```shell
   cat "$(subagent-router paths --json | python3 -c 'import sys,json; print(json.load(sys.stdin)["provider_error_log_dir"])')"/*.json
   ```

4. Verify the provider endpoint is reachable:
   ```shell
   subagent-router doctor
   ```

5. Test with a minimal mock request:
   ```shell
   subagent-router start --mock --port 8787 &
   curl -sS http://127.0.0.1:8787/v1/responses \
     -H 'Content-Type: application/json' \
     -d '{"model":"deepseek-chat","stream":false,"input":"Hello","tools":[]}'
   ```

## See Also

- [Usage and Configuration](usage.md) — environment variables, CLI commands,
  and configuration reference
- [Test Matrix](test_matrix.md) — current and planned test coverage for all
  providers and features
- [ROADMAP.md](ROADMAP.md) — full engineering roadmap with phases and provider
  roadmap
- [Troubleshooting](troubleshooting.md) — health checks, diagnostics, and
  common provider issues
