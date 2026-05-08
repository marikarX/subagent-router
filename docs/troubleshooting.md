# Troubleshooting

## Basic Health

Start the proxy in mock mode:

```shell
subagent-router start --mock --port 8787
```

In another shell:

```shell
curl -sS http://127.0.0.1:8787/health
curl -sS http://127.0.0.1:8787/debug/activity
subagent-router paths
```

If `subagent-router doctor` fails, check whether `DEEPSEEK_API_KEY` is set or use mock
mode:

```shell
subagent-router doctor --mock
```

## Provider Errors

Run with compact trace output:

```shell
DEEPSEEK_API_KEY=... subagent-router start --trace
```

Trace output shows request/response status, message previews, tool-call
summaries, token totals, and provider diagnostic paths. It does not print raw
provider prompts or private `reasoning_content`.

Provider error diagnostics are written under the configured
`provider_error_log_dir`. Find the resolved path with:

```shell
subagent-router paths
```

Diagnostics include requested/upstream model names, status code, redacted
provider body, tool counts, forwarded tool names, dropped tool names, and input
shape. They omit authorization headers, API keys, cookies, passwords, request
headers, full prompt payloads, and full diffs.

## Subagent Wait Fallback

When a DeepSeek-backed Codex subagent appears stuck, inspect activity:

```shell
curl -sS http://127.0.0.1:8787/debug/activity
```

If the shell cannot reach the proxy over loopback, use file paths:

```shell
subagent-router paths
```

Read the reported `activity_file` and `session_mirror_file`. If activity is
recent and `error_count` has not increased, the provider may still be working.
If `session_mirror.final` contains messages but Codex did not surface a final
agent result, report the mirrored final messages and stop instead of silently
continuing locally.

If `wait_agent` returns a final or complete response that is empty, null, or
only a progress line such as `Now I'll fix both call sites:`, send `continue`
or a concise instruction to the same agent with `send_input` asking it to finish
with changed files, tests run, and results. Wait once more. If the agent repeats
invalid final output, stop and report the trace id plus sanitized
`session_mirror.latest`/`session_mirror.final` details.

## Tool Filtering

The proxy forwards supported function tools only.

- `exec_command` is preserved.
- Browser-style tools and unsupported Responses built-ins are dropped.
- `apply_patch` is dropped for read-only reviewer requests.
- `apply_patch` is preserved for worker aliases such as
  `subagent-router-worker` and `deepseek-worker`, write-capable metadata, or
  `DEEPSEEK_ALLOW_APPLY_PATCH=1`.
- Write-capable worker aliases keep `apply_patch` by default.

## Thinking-Mode Continuations

DeepSeek V4 Pro can require private `reasoning_content` to be replayed after a
tool call. The proxy keeps that value in process memory, keyed by tool call id,
and reattaches it for the matching tool-result continuation. It is not returned
to Codex and is omitted from diagnostics.

If this fails after a proxy restart, retry the request from the beginning; the
MVP continuation cache is not durable.

## Budget Exceeded (402 Payment Required)

If the proxy returns a `402 Payment Required` error, it means a configured budget limit has been reached in `hard-stop` mode.

1. **Check usage**:
   ```shell
   subagent-router usage
   ```
2. **Identify the limit**:
   Review your `config.toml` or environment variables for `MAX_COST_*` or `MAX_TOKENS_*` settings.
3. **Reset daily limits**:
   Daily limits reset at 00:00 UTC. If you need to override for the rest of the day, increase the limit in your configuration and restart the proxy.
4. **Temporary override**:
   You can start the proxy with a higher limit for a single session:
   ```shell
   SUBAGENT_ROUTER_MAX_COST_PER_DAY=10.00 subagent-router restart
   ```

## Debug Bundle

If you need to report an issue without exposing secrets:

```shell
subagent-router debug-bundle
```

This creates a `subagent-router-debug-*.tar.gz` archive containing logs and diagnostics with sensitive headers and prompt content redacted.
