# Subagent Router Test Matrix

## Current Coverage

### Unit Tests

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
- Settings parsing: defaults resolve under `$XDG_STATE_HOME/subagent-router`
  or `~/.local/state/subagent-router`.
- Settings parsing: `XDG_STATE_HOME` takes precedence over `HOME`.
- Settings parsing: relative path overrides resolve under `state_dir`.
- Settings parsing: `~` expansion and absolute paths pass through.
- Settings parsing: all env vars load correctly.
- Settings `as_env()`: excludes `DEEPSEEK_API_KEY` by default, includes on opt-in.
- Settings `as_env()`: omits API key env entry when none is set.
- Settings: legacy `CODEX_PROXY_*` aliases still work.
- CLI `paths`: resolved paths are printed as JSON.
- CLI `doctor`: succeeds in mock mode without API key.
- CLI `doctor`: fails when neither mock nor API key is set.
- CLI `init`: default mode installs instructions, references from `AGENTS.md`,
  writes agent role files and provider config.
- CLI `init`: preserves existing `AGENTS.md` content below the router path.
- CLI `init`: replaces legacy bare router path in `AGENTS.md`.
- CLI `init --mode opt-in`: installs skill, slash command, agent roles, and provider config;
  no global instructions or AGENTS.md reference.
- CLI `init --mode provider-only`: installs only provider config and agent roles.
- CLI `init`: idempotent on same version.
- CLI `init --force`: overwrites user modifications.
- CLI `init`: marked config block is replaced, outer content preserved.
- CLI `init --profile cost-optimization`: selects the default profile, writes matching
  `SUBAGENT_ROUTER_INSTRUCTIONS.md`, and persists `delegation_profile` in manifest.
- CLI `init --profile deep-delegation`: selects the deep-delegation profile with
  corresponding instructions and manifest entry.
- CLI `init --profile orchestrator`: selects the orchestrator profile with
  corresponding instructions and manifest entry.
- CLI `init --profile manual`: records the manual profile, installs provider config
  and role files, and does not install global automatic delegation instructions.
- CLI `init --profile` with an invalid value exits with code 2 and prints the
  accepted profiles and aliases.
- CLI `init --profile` aliases (`cost`, `budget`, `token-optimized`, `deep`,
  `aggressive`, `quality`, `conservative`, `opt-in`, `provider-only`) resolve
  to their canonical profile.
- CLI `init --profile` only affects `--mode default`; `opt-in` and `provider-only` modes
  ignore the profile argument.
- CLI `init`: agent role files (`subagent-router-{explorer,worker,reviewer}.toml`) are
  written to `~/.codex/agents/` with correct agent names and sandbox modes.
- CLI `init`: agent role files contain expected agent names
  (`subagent_router_explorer`, `subagent_router_worker`, `subagent_router_reviewer`).
- CLI `init`: explorer, worker, and reviewer role files include compact output
  contracts and DeepSeek role model aliases.
- CLI `paths` displays the active delegation profile; `doctor --json` includes it.
- API `/v1/config` includes the active delegation profile when available.
- CLI `tui`: show delegation profile in config info panel.
- CLI `run`: strips `DEEPSEEK_API_KEY` from child process env.
- CLI `run`: strips configured secret keys from child process env.

### Integration Tests

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

### Failure Tests

- Assistant tool call followed by a user message without tool output returns
  structured 400.
- Tool output with a mismatched `call_id` returns structured 400.
- Tool output with no `call_id` returns structured 400.
- Provider 4xx response is returned as a structured provider rejection without
  logging secrets.
- Provider transport failure returns 502.
- Request body exceeds 10 MB returns 413 with structured error.

### Live Codex Validation

Validate the proxy against a real Codex session after unit and synthetic
integration tests pass:

1. Configure Codex custom provider to point at the local proxy.
2. Spawn a low-cost subagent with repo read-only exploration.
3. Confirm Codex receives `function_call` items, executes `exec_command`, sends
   matching outputs, and receives a final assistant message followed by
   `response.completed`.
4. Repeat with a bounded worker task that can use `apply_patch`.
5. Run one `subagent-router run -- codex ...` command to verify ephemeral
   startup and shutdown.

## Manual Test Plan

Use this plan for release validation after the automated test suite passes.
Record command output, the state directory used, provider names, and any trace
IDs for failures.

### 1. Automated Gate

x Run `pytest -q`.
x Run `git diff --check`.
x Run `subagent-router validate-artifacts --json`.
  Example:

  ```shell
  subagent-router validate-artifacts --json | python -m json.tool
  python - <<'PY'
  import json
  import subprocess

  data = json.loads(subprocess.check_output(["subagent-router", "validate-artifacts", "--json"]))
  assert data["healthy"], data
  assert data["package_version"] == data["pyproject_version"], data
  PY
  ```

x Confirm `package.json` and `pyproject.toml` report the same version.
x Confirm generated files are not included in `git status --short`:
  `__pycache__/`, `*.pyc`, `*.egg-info/`, `*.tgz`, local state, local logs,
  provider error diagnostics, and `scripts/local-*.sh`.

### 2. Offline Mock Smoke

x Run `subagent-router doctor --mock`.
x Run `subagent-router paths --json --mock` and verify provider summaries,
  audit path, usage path, and usage JSONL path are present.
x Start a mock proxy with an isolated state directory:

  ```shell
  STATE_DIR=/tmp/subagent-router-manual
  rm -rf "$STATE_DIR"
  subagent-router start --mock --background --state-dir "$STATE_DIR" --port 8787
  subagent-router status --state-dir "$STATE_DIR"
  ```

x Call `/health`, `/debug/activity`, `/debug/paths`, and `/debug/config`:

  ```shell
  curl -sS http://127.0.0.1:8787/health | python -m json.tool
  curl -sS http://127.0.0.1:8787/debug/activity | python -m json.tool
  curl -sS http://127.0.0.1:8787/debug/paths | python -m json.tool
  curl -sS http://127.0.0.1:8787/debug/config | python -m json.tool
  ```

x POST a minimal non-streaming `/v1/responses` request and verify a Responses
  object with provider metadata is returned:

  ```shell
  curl -sS http://127.0.0.1:8787/v1/responses \
    -H 'Content-Type: application/json' \
    -d '{"model":"deepseek-chat","stream":false,"input":"Say hello from manual smoke","tools":[]}' \
    > /tmp/subagent-router-manual/non-streaming-response.json

  python - <<'PY'
  import json
  from pathlib import Path

  data = json.loads(Path("/tmp/subagent-router-manual/non-streaming-response.json").read_text())
  assert data["object"] == "response", data
  assert data["output"], data
  metadata = data["metadata"]
  assert metadata["proxy"] == "subagent-router", metadata
  assert metadata["provider"], metadata
  assert metadata["provider_model"], metadata
  assert metadata["routing_policy"], metadata
  PY
  ```

x POST a streaming request and verify `response.created`,
  `response.output_item.done`, and `response.completed` events are emitted:

  ```shell
  curl -sSN http://127.0.0.1:8787/v1/responses \
    -H 'Content-Type: application/json' \
    -d '{"model":"deepseek-chat","stream":true,"input":"Stream hello from manual smoke","tools":[]}' \
    > /tmp/subagent-router-manual/streaming-response.sse

  rg 'event: response.created' /tmp/subagent-router-manual/streaming-response.sse
  rg 'event: response.output_item.done' /tmp/subagent-router-manual/streaming-response.sse
  rg 'event: response.completed' /tmp/subagent-router-manual/streaming-response.sse
  ```

x Stop the background proxy and verify `subagent-router status` reports it is
  not running:

  ```shell
  subagent-router stop --state-dir "$STATE_DIR"
  subagent-router status --state-dir "$STATE_DIR"
  ```

### 3. Provider Configuration

x Create a temporary config file with `[defaults]`, `[providers.deepseek]`,
  `[providers.openai-compatible]`, `[providers.ollama]`, `[routes.*]`,
  `[budgets]`, `[security]`, and `[predictions.*]`.
  Example:

  ```shell
  CONFIG=/tmp/subagent-router-manual/config.toml
  cat > "$CONFIG" <<'TOML'
  [defaults]
  provider = "deepseek"
  fallback_providers = ["ollama"]

  [providers.deepseek]
  type = "deepseek"
  kind = "cloud"
  base_url = "https://api.deepseek.com/v1"
  model = "deepseek-chat"

  [providers.openai-compatible]
  type = "openai-compatible"
  kind = "cloud"
  enabled = true
  base_url = "http://127.0.0.1:9999/v1"
  api_key = "sk-manual-placeholder"
  model = "manual-compatible-model"

  [providers.ollama]
  type = "ollama"
  kind = "local"
  enabled = true
  base_url = "http://127.0.0.1:11434"
  model = "llama3.1"

  [routes.cheap-review]
  provider = "deepseek"
  fallback_providers = ["ollama"]
  TOML

  SUBAGENT_ROUTER_CONFIG="$CONFIG" subagent-router paths --json --mock | python -m json.tool
  ```

x Verify config precedence: command-line flag overrides environment,
  environment overrides project config, project config overrides user config,
  and user config overrides defaults.
  Example:

  ```shell
  HOME_DIR=/tmp/subagent-router-manual/home
  PROJECT_DIR=/tmp/subagent-router-manual/project
  mkdir -p "$HOME_DIR/.config/subagent-router" "$PROJECT_DIR"

  cat > "$HOME_DIR/.config/subagent-router/config.toml" <<'TOML'
  [defaults]
  provider = "ollama"
  TOML

  cat > "$PROJECT_DIR/.subagent-router.toml" <<'TOML'
  [defaults]
  provider = "deepseek"
  TOML

  (cd "$PROJECT_DIR" && HOME="$HOME_DIR" subagent-router doctor --mock --json)
  (cd "$PROJECT_DIR" && HOME="$HOME_DIR" SUBAGENT_ROUTER_PROVIDER=ollama subagent-router doctor --mock --json)
  (cd "$PROJECT_DIR" && HOME="$HOME_DIR" SUBAGENT_ROUTER_PROVIDER=ollama subagent-router doctor --mock --json --provider deepseek)
  ```

x Run `doctor` for DeepSeek with mock mode, OpenAI-compatible with a configured
  base URL, and Ollama with `--ollama-enabled`.
  Example:

  ```shell
  subagent-router doctor --mock --provider deepseek
  subagent-router doctor --mock --provider openai-compatible \
    --openai-compatible-base-url http://127.0.0.1:9999/v1 \
    --openai-compatible-api-key sk-manual-placeholder \
    --openai-compatible-model manual-compatible-model
  subagent-router doctor --mock --provider ollama --ollama-enabled
  ```

x Verify disabled, unknown, allowlisted, and denylisted providers return
  structured errors.
  Example:

  ```shell
  subagent-router doctor --mock --provider unknown-provider || true
  subagent-router start --mock --background --state-dir "$STATE_DIR" --port 8787 \
    --provider-denylist deepseek
  curl -sS http://127.0.0.1:8787/v1/responses \
    -H 'Content-Type: application/json' \
    -d '{"model":"deepseek-chat","stream":false,"input":"denylist check","tools":[]}' \
    | python -m json.tool
  subagent-router stop --state-dir "$STATE_DIR"
  ```

x Verify config permission warnings are reported for overly broad config-file
  permissions.
  Example:

  ```shell
  chmod 644 "$CONFIG"
  SUBAGENT_ROUTER_CONFIG="$CONFIG" subagent-router start --mock --background \
    --state-dir "$STATE_DIR" --port 8787
  curl -sS http://127.0.0.1:8787/debug/config | python -m json.tool
  subagent-router stop --state-dir "$STATE_DIR"
  chmod 600 "$CONFIG"
  ```

### 4. Routing And Fallbacks

x Verify default routing returns `routing_policy = safe-default`.
x Verify manual provider override via query parameter, header, and metadata.
x Verify built-in policies: `cheap-review`, `local-only`, `high-context`,
  `fast-draft`, and `budget-capped`.
x Configure a route with a primary provider and fallback provider; force the
  primary to return a transport error or 5xx and verify fallback succeeds.
x Force a provider 4xx and verify fallback does not run.
x Confirm usage and audit records include selected provider, model, policy,
  selection reason, latency, estimated cost, and fallback chain.
x Configure predictions or health metadata that prefer a fallback candidate and
  verify automatic provider ordering selects the higher-scored provider.

### 5. Budget Controls

x In warn mode, exceed a per-task token limit and verify the request completes
  while a budget warning is written to the audit log.
x In hard-stop mode, exceed per-task cost and token limits and verify a
  structured 402 response before or after provider response as appropriate.
x Verify per-provider and per-model budget limits override broader task limits.
x Verify session cost and token limits apply across multiple requests in one
  proxy process.
x Verify daily cost and token limits are read from the usage summary and apply
  across proxy restarts.
x Verify `max_spend_*` aliases behave the same as the matching `max_cost_*`
  settings.

### 6. Usage, Audit, And Debugging

x Generate at least one successful request and one provider failure.
x Run `subagent-router usage` and `subagent-router usage --json`; verify totals,
  provider breakdown, model breakdown, token counts, and estimated cost.
x Run `subagent-router logs --audit`; verify success, warning, retry, fallback,
  and failure records are present when triggered.
x Run `subagent-router debug-bundle --output /tmp/subagent-router-debug.tar.gz`
  and verify the archive contains activity, session mirror, audit, usage,
  provider diagnostics, and server log files when present.
x Inspect the bundle and provider diagnostics for redaction of API keys,
  authorization headers, cookies, passwords, prompt/message bodies,
  `reasoning_content`, tool calls, and tool-call arguments.

### 7. Handoff Integrations

x Run `subagent-router stdio --mock` with one valid Responses JSON payload and
  verify stdout contains only one Responses JSON object.
x Run `stdio` with invalid JSON and verify the error is written to stderr with
  a nonzero exit code.
x Run `subagent-router handoff --input-dir <tasks> --output-dir <results> --once
  --mock` with valid and invalid task files.
x Verify valid tasks produce `<stem>.response.json` and invalid tasks produce
  `<stem>.error.json`.
x Verify handoff metadata can select provider, model, and routing policy.

### 8. Subagent Loop Behavior

x Send a worker request with `apply_patch` and verify the tool is preserved by
  default for write-capable worker aliases.
x Send a reviewer request with `apply_patch` and verify the tool is dropped.
x Force an empty provider final message and verify the proxy returns a
  structured error for non-subagent models.
x Force a subagent empty final message or progress-only final line and verify
  one retry is attempted.
x Force repeated incomplete subagent output with `exec_command` available and
  verify the proxy synthesizes a continuation tool call with `end_turn = false`.
x Verify provider `reasoning_content` is preserved for replay but omitted from
  Responses output and trace logs.

### 9. Live Provider Validation

x DeepSeek: run a real non-streaming text request, a streaming request, and an
  `exec_command` tool-call loop.
x OpenAI-compatible: run the same three requests against a configured compatible
  endpoint and verify model, usage, and error mapping.
x Ollama: run a local text request and, when supported by the model, a tool-call
  request; verify zero API cost and local request counts.
- Validate provider-specific failure modes: bad API key, unreachable endpoint,
  timeout, unavailable local model, and malformed provider response.

### 10. Codex Integration

x Run `subagent-router init` in a temporary `CODEX_HOME` and verify
  `AGENTS.md`, `SUBAGENT_ROUTER_INSTRUCTIONS.md`, agent role files,
  config block, and manifest are created.
x Run `init` a second time and verify it is idempotent.
x Modify a managed file and verify `init` preserves the user edit unless
  `--force` is supplied.
x Run `init --mode opt-in` and verify only opt-in activation files are written.
x Run `init --mode provider-only` and verify only provider config and agent role
  files are written.
x Run `subagent-router run -- codex ...` and verify temporary provider
  overrides are injected and secret environment variables are stripped from the
  child process.

#### Codex v0.130.0 Compatibility Smoke

Validated against installed `codex-cli 0.130.0` with package version `0.2.4`.
Compatibility scope: OpenAI Codex custom model provider over local
`/v1/responses`, not app-server or desktop SDK internals. Codex v0.128.0 and
v0.130.0 are the currently recorded tested versions for this integration.

Commands and results:

- `codex --version` -> `codex-cli 0.130.0`
- `python --version` -> `Python 3.12.3`
- `python -m pytest -q` -> 216 passed
- `UV_CACHE_DIR=/tmp/subagent-router-uv-cache uv run pytest -q` -> 216 passed,
  7 subtests passed
- `subagent-router doctor --mock` -> passed
- `subagent-router validate-artifacts` -> passed
- `git diff --check` -> passed
- `subagent-router start --mock --state-dir /tmp/subagent-router-codex-0130-smoke-fg --port 18788`
  plus `curl -sS http://127.0.0.1:18788/health` -> passed
- Non-streaming `POST /v1/responses` to the mock router -> returned a
  Responses object with assistant output and provider metadata
- Streaming `POST /v1/responses` to the mock router -> emitted
  `response.created`, `response.output_item.done`, and `response.completed`
- Function-tool-shaped `POST /v1/responses` with `exec_command` -> returned a
  `function_call` item with `call_id = "call_mock_1"` and `end_turn = false`
- Follow-up `POST /v1/responses` with `previous_response_id` and
  `function_call_output` -> accepted and returned a valid Responses object
- `subagent-router init --codex-home /tmp/subagent-router-codex-home-0130 --profile cost-optimization`
  -> wrote `AGENTS.md`, `SUBAGENT_ROUTER_INSTRUCTIONS.md`, all three
  `agents/subagent-router-*.toml` role files, manifest, and a managed
  `[model_providers.subagent_router]` block using
  `base_url = "http://127.0.0.1:8787/v1"` and `wire_api = "responses"`
- `CODEX_HOME=/tmp/subagent-router-codex-home-0130 subagent-router run --mock --state-dir /tmp/subagent-router-codex-run-0130 -- codex exec --skip-git-repo-check --ephemeral --ignore-rules -c model_provider="subagent_router" -m deepseek-chat "Reply with exactly: router-smoke-ok"`
  -> passed; Codex reported provider `subagent_router` and returned
  `router-smoke-ok`

### 11. Terminal And Release Tools

x Run `subagent-router tui` against empty state and populated activity/usage
  files.
x Run `subagent-router tui --watch` in a terminal and verify it refreshes and
  exits cleanly on interrupt.
x Run `subagent-router validate-artifacts` and
  `subagent-router validate-artifacts --json` from the source tree.
- Build or dry-run package artifacts when release tooling is available and
  verify docs, `bin/`, source files, and changelog are included while caches and
  local artifacts are excluded.

## Roadmap Coverage

The sections below map roadmap phases to implemented and remaining tests.

### Multi-Provider Tests (Phase 1)

#### Provider Abstraction Contract

A shared contract suite that every provider adapter must pass:

x Provider is selectable via `SUBAGENT_ROUTER_PROVIDER` env var.
x Provider is selectable via config file `[defaults] provider` field.
x Unknown provider value returns a structured startup error.
x Provider can be overridden per-request via metadata or header.

#### OpenAI-Compatible Provider

x Generic OpenAI-compatible provider accepts configurable `base_url`,
  `api_key`, and `model`.
x Provider supports non-streaming upstream requests and Responses SSE output.
x Provider maps Responses input to chat/completions format.
x Provider maps tool calls, tool results, and function call output.
x Provider supports configurable timeout and retry behavior.
x Provider errors map to structured proxy errors.

#### Ollama Provider

x Ollama provider starts a local model without an API key.
x Ollama provider accepts configurable `base_url` and `model`.
x Model capability overrides work for incomplete Ollama model metadata.
x Missing local endpoint returns a structured error with fallback hint.
x Fallback triggers when the selected Ollama model is unavailable.
x Provider supports non-streaming upstream requests and Responses SSE output.
x Local model usage is tracked separately from cloud provider usage
  (token estimates, latency, request count, failures).
x Local API cost is reported as zero while utilization is still tracked.

### Routing Policy Tests (Phase 3)

#### Policy Selection

x Default policy applies when no policy is configured.
x Policy is selectable via request metadata, headers, or query parameters.
x Policy is selectable via config file.
x Invalid policy name returns a structured startup error.
x Policy can be overridden per-request.

#### Built-in Policies

x `safe-default` selects the configured active provider.
x `cheap-review` selects the configured cheap-review route or DeepSeek default.
x `local-only` selects Ollama without fallback.
x `high-context`, `fast-draft`, and `budget-capped` have deterministic MVP
  routes; historical latency and capability optimization remain future work.

#### Manual Provider Override

x Manual override via query parameter, header, metadata, or
  `SUBAGENT_ROUTER_PROVIDER` bypasses policy.
x Manual override is reflected in audit log as selection reason.
x Manual override of an unconfigured provider returns a startup error.

#### Fallback Behavior

x Configured fallback provider is selected when the primary provider returns a
  transport error.
x Configured fallback provider is selected when the primary provider returns a
  5xx status.
x Provider 4xx errors do not trigger fallback (client error, not provider).
x Fallback chain respects the same budget constraints.
x Successful fallback increments activity counters and is included in the
  success audit record. Full failed-chain audit detail remains future work.

### Usage and Audit Log Tests (Phase 5–6)

#### Usage Tracking

x Per-request usage record is written to a structured JSON summary file.
x Usage record includes provider name, model name, prompt/completion/total
  tokens.
x Usage record includes estimated cost when pricing metadata is available.
x Usage record includes latency, success/failure, and trace ID.
x Usage record includes selection reason (policy or override).
x Local model usage is recorded with zero API cost but nonzero token estimates.
x Usage records are bounded to recent entries; atomic writes remain future work.

#### Usage Summary

x `subagent-router usage` prints total tokens, cost, request count, provider
  breakdown, and model breakdown.
x Usage summary handles empty logs gracefully.
x Usage summary truncates to a configurable time window.

### Budget Control Tests (Phase 5)

#### Per-Task Budget

x Task exceeding `max_cost_per_task` is rejected before provider submission.
x Task exceeding `max_tokens_per_task` is rejected before provider submission.
x Warn-only mode returns the provider result but records a warning in the audit
  log.
x Hard-stop mode returns a structured 402.
x Per-task budget applies to all providers in a fallback chain.

#### Session/Daily Budget

x Session cumulative cost exceeding the limit triggers configured mode
  (warn-only or hard-stop).
x Daily cumulative cost exceeding the limit triggers configured mode.
x Budget counters reset at session or daily boundary.
x Per-provider and per-model budget limits are enforced independently.
x Budget state persists across proxy restarts when configured.

### Debug Bundle Tests (Phase 6)

x `subagent-router debug-bundle` writes a valid archive without errors.
x Debug bundle includes redacted activity logs.
x Debug bundle includes redacted provider error diagnostics.
x Debug bundle includes activity, session mirror, audit log, usage summary,
  provider error diagnostics, and server log when present.
x Sensitive fields (API keys, auth headers, prompt bodies, `reasoning_content`)
  are redacted or omitted from the bundle.
x Debug bundle does not contain raw, unredacted payloads.
x Debug bundle overwrites the requested output path.

### Stdin/Stdout Handoff Tests (Phase 4)

x Stdin/stdout handoff reads a task specification from stdin.
x Stdin/stdout handoff writes one JSON response to stdout.
x Stdin/stdout handoff applies the configured routing policy.
x Stdin/stdout handoff respects budget constraints.
x Stdin/stdout handoff errors are written to stderr, not mixed with stdout.
x File-based handoff processes configured directory task files.
x File-based handoff writes result files with matching input stems.
x File-based handoff writes error files for invalid task files.

### Configuration and Secrets Tests

x Config file `[defaults]`, `[providers.*]`, `[routes.*]`, and `[budgets]`
  sections are loaded when present.
x Environment variables override config file values.
x Command-line flags override environment variables.
x Invalid config file returns a structured parse error with line number.
x Secrets are never written to usage logs, debug bundles, or provider error
  diagnostics.
x `SUBAGENT_ROUTER_TRACE` output does not contain raw API keys or auth headers.

### Terminal UI Tests (Phase 11)

x `subagent-router tui` renders correctly with no activity or usage files.
x `subagent-router tui` renders health status, provider names, and cost/token totals.
x `subagent-router tui --watch` refreshes the view every 2 seconds.
x Terminal UI uses ANSI colors when connected to a TTY.
x Exiting the TUI (Ctrl+C) returns a clean shell prompt.

## See Also

- [Usage and Configuration](usage.md) — configuration reference, environment
  variables, and CLI commands
- [Compatibility Matrix](compatibility.md) — supported providers, models, and
  capabilities
- [ROADMAP.md](ROADMAP.md) — full engineering roadmap with phases
