# Usage And Configuration

## Installation

From npm:

```shell
npm install -g subagent-router
```

From a local checkout:

```shell
python -m venv .venv
. .venv/bin/activate
pip install -e '.[server]'
```

To test the npm package before publishing:

```shell
npm pack
npm install -g ./subagent-router-0.1.2.tgz
```

The npm package exposes both `subagent-router` and the short `sar` alias
via `bin/subagent-router.js`, which auto-detects Python 3.11+.

## Commands

| Command | Description |
|---|---|
| `subagent-router init` | Install Codex integration files |
| `subagent-router start` | Start the local HTTP proxy |
| `subagent-router start --background` | Start the proxy in the background |
| `subagent-router stop` | Stop a background proxy |
| `subagent-router restart` | Restart a background proxy |
| `subagent-router status` | Print background proxy status |
| `subagent-router logs --follow` | Print or follow the background proxy log |
| `subagent-router logs --audit` | Print the structured audit JSONL log |
| `subagent-router run -- <cmd>` | Start proxy for one command |
| `subagent-router stdio` | Read one Responses JSON request from stdin and write JSON response |
| `subagent-router handoff` | Process JSON task files from a directory |
| `subagent-router tui` | Print a compact terminal status view; `--watch` for live refresh |
| `subagent-router usage` | Print usage and cost summary |
| `subagent-router debug-bundle` | Write a redacted troubleshooting archive |
| `subagent-router doctor` | Check local proxy configuration |
| `subagent-router paths` | Print resolved proxy paths |
| `subagent-router version` | Print the package version |
| `subagent-router validate-artifacts` | Validate release artifacts for packaging consistency |
| `subagent-router install-service` | Write a systemd user service |

### init

Default mode writes:

- `~/.codex/SUBAGENT_ROUTER_INSTRUCTIONS.md`
- `Follow instructions in ...` at the top of `~/.codex/AGENTS.md`
- `~/.codex/agents/subagent-router-worker.toml`
- `~/.codex/agents/subagent-router-reviewer.toml`
- a managed `subagent_router` provider block in `~/.codex/config.toml`

Optional modes:

```shell
subagent-router init --mode opt-in
subagent-router init --mode provider-only
```

`--mode opt-in` writes the `$deepseek` skill and `/deepseek` slash command
instead of `SUBAGENT_ROUTER_INSTRUCTIONS.md` or an `AGENTS.md` path reference.

`--mode provider-only` writes only the provider config and agent role files.

The md/toml templates are bundled in the package under
`src/subagent_router/templates/`.

### start

```shell
# Foreground (current terminal)
DEEPSEEK_API_KEY=... subagent-router start

# Deterministic mock responses (no API key required)
subagent-router start --mock

# Background daemon with attached log tail
DEEPSEEK_API_KEY=... subagent-router start --background --attach-logs
```

The background process writes its PID to
`$SUBAGENT_ROUTER_STATE_DIR/subagent-router.pid` and server output to
`$SUBAGENT_ROUTER_STATE_DIR/logs/server.log`.

### run

```shell
DEEPSEEK_API_KEY=... subagent-router run -- codex
```

`run --` starts the proxy on a free loopback port when `--port` is omitted,
injects temporary Codex provider overrides with `-c`, runs the requested
command, and stops the proxy when the command exits. The child process does
**not** receive `DEEPSEEK_API_KEY` in its environment to avoid leaking secrets
to the orchestrated command.

### doctor

```shell
subagent-router doctor                 # requires DEEPSEEK_API_KEY or --mock
subagent-router doctor --mock          # offline validation
subagent-router doctor --json          # machine-readable output
```

Checks that the configured provider endpoint, state directory, Python
environment, and optional Codex home are usable.

### install-service

```shell
subagent-router install-service                            # write a systemd user unit
subagent-router install-service --name subagent-router     # override service name
subagent-router install-service --force                    # overwrite existing unit
```

Writes a systemd user service unit to
`~/.config/systemd/user/<name>.service` so the proxy can start at session
login or be managed with `systemctl --user`.

### paths

```shell
subagent-router paths         # human-readable table
subagent-router paths --json  # machine-readable JSON
```

Prints all resolved state, log, activity, and diagnostic paths.

## Environment Variables

### Provider Configuration

- `DEEPSEEK_API_KEY`: required unless mock mode is enabled
- `DEEPSEEK_BASE_URL`: defaults to `https://api.deepseek.com/v1`
- `DEEPSEEK_MODEL`: optional upstream model override
- `DEEPSEEK_SEND_PARALLEL_TOOL_CALLS=1`: forwards `parallel_tool_calls`
- `DEEPSEEK_PROXY_MOCK=1`: returns deterministic local responses
- `DEEPSEEK_ALLOW_APPLY_PATCH=1`: forwards `apply_patch` for non-worker
  requests
- `SUBAGENT_ROUTER_PROVIDER`: default provider name (`deepseek`,
  `openai-compatible`, or `ollama`)
- `SUBAGENT_ROUTER_FALLBACK_PROVIDERS`: comma-separated fallback provider names
- `OPENAI_COMPAT_BASE_URL`, `OPENAI_COMPAT_API_KEY`, `OPENAI_COMPAT_MODEL`:
  generic OpenAI-compatible `/chat/completions` provider configuration
- `OLLAMA_ENABLED=1`, `OLLAMA_BASE_URL`, `OLLAMA_MODEL`: local Ollama provider
  configuration

### Server Configuration

- `SUBAGENT_ROUTER_HOST`: defaults to `127.0.0.1`
- `SUBAGENT_ROUTER_PORT`: defaults to `8787`
- `SUBAGENT_ROUTER_STATE_DIR`: defaults to
  `$XDG_STATE_HOME/subagent-router`, or
  `~/.local/state/subagent-router` when `XDG_STATE_HOME` is unset
- `SUBAGENT_ROUTER_TRACE=1`: prints compact request/response trace lines

The older `CODEX_PROXY_*` names are still accepted as compatibility aliases for
the Codex integration.

### Log and Diagnostics Paths

All paths support relative, absolute, and `~`-prefixed values. Relative paths
resolve under `SUBAGENT_ROUTER_STATE_DIR` unless absolute. Resolved paths are
exposed by `subagent-router paths`.

- `SUBAGENT_ROUTER_LOG_DIR`: defaults to `logs/client_payloads` under state dir
- `SUBAGENT_ROUTER_ACTIVITY_FILE`: defaults to `logs/activity.json` under state dir
- `SUBAGENT_ROUTER_SESSION_MIRROR_FILE`: defaults to `logs/session_mirror.json`
  under state dir
- `SUBAGENT_ROUTER_PROVIDER_ERROR_LOG_DIR`: defaults to `logs/provider_errors`
  under state dir
- `SUBAGENT_ROUTER_AUDIT_LOG_FILE`: defaults to `logs/audit.jsonl` under state dir
- `SUBAGENT_ROUTER_USAGE_FILE`: defaults to `logs/usage.json` under state dir
- `SUBAGENT_ROUTER_USAGE_JSONL_FILE`: defaults to `logs/usage.jsonl` under state dir

### Budget Controls

- `SUBAGENT_ROUTER_MAX_COST_PER_TASK`: warn or stop when a single response exceeds this USD cost
- `SUBAGENT_ROUTER_MAX_TOKENS_PER_TASK`: warn or stop when a single request/response exceeds this token budget
- `SUBAGENT_ROUTER_MAX_COST_PER_SESSION`: aggregate cost limit for the current proxy process
- `SUBAGENT_ROUTER_MAX_TOKENS_PER_SESSION`: aggregate token limit for the current proxy process
- `SUBAGENT_ROUTER_MAX_COST_PER_DAY`: aggregate cost limit across all proxy processes for the current UTC day
- `SUBAGENT_ROUTER_MAX_TOKENS_PER_DAY`: aggregate token limit across all proxy processes for the current UTC day
- `SUBAGENT_ROUTER_BUDGET_MODE`: `warn` (default) or `hard-stop`. In `hard-stop` mode, the proxy returns `402 Payment Required` when a budget is exceeded.

**Aliases**: The following aliases are also supported in both environment variables and `config.toml`:
- `max_spend_*` (alias for `max_cost_*`)
- `max_budget_*` (alias for `max_cost_*`)

### Routing and Security
- `SUBAGENT_ROUTER_PROVIDER_ALLOWLIST`: comma-separated provider allowlist
- `SUBAGENT_ROUTER_PROVIDER_DENYLIST`: comma-separated provider denylist
- `SUBAGENT_ROUTER_DRY_RUN=1`: normalize, route, and respond without calling a provider

## API Endpoints

### `/v1/responses` (POST)

The primary Codex-compatible Responses endpoint. Accepts `model`,
`instructions`, `input`, `tools`, `tool_choice`, `stream`, `metadata`, and
`previous_response_id`. Normalizes the request to the selected provider format,
calls the provider, and returns Responses-compatible JSON or SSE.

Per-request provider override is supported with query parameters, headers, or
metadata:

```shell
curl -sS 'http://127.0.0.1:8787/v1/responses?provider=ollama&model=llama3.1' \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-chat","stream":false,"input":"Say hello","tools":[]}'
```

### `/health` (GET)

Returns `status`, default provider, and sanitized configured provider metadata
when the proxy is running.

### `/debug/activity` (GET)

Returns an in-memory activity summary: request count, response count, error
count, last model, last trace ID, last output kind, and timestamps. This data
is ephemeral and resets on proxy restart.

### `/debug/paths` (GET)

Returns the same resolved path information as `subagent-router paths --json`.

## API Request Limits

- Maximum request body size: 10 MB
- Maximum in-memory response state entries (for `previous_response_id`
  continuations): 100
- Maximum session mirror events: 200

## Behavior

### Tool Normalization

Only `function`-type tools are forwarded to the provider. These tool types
are **dropped** before provider submission:

- `image_generation`
- `local_shell`
- `tool_search`
- `web_search`
- `custom`
- Namespace tools with `browser_`-prefixed children
- `apply_patch` (for reviewer requests unless explicitly allowed)

### Content Normalization

- `developer` message role becomes `system` (DeepSeek compatibility).
- `input_image` content items are replaced with an `[Image omitted]` marker.
- Text content items are flattened to chat message text.

### Streaming

For successful streaming responses, the proxy emits:

1. `response.created`
2. zero or more `response.output_item.done`
3. `response.completed`

When output contains tool calls, `response.end_turn` is `false` so the
primary agent keeps the tool loop moving.

### Previous Response ID

The proxy accepts `previous_response_id` to continue a prior multi-turn
conversation. The continuation cache is **in-memory and process-local**; it is
lost on proxy restart. The maximum number of cached response states is 100.

### Secrets Handling

Provider error diagnostics and logged payloads have these fields redacted:
authorization headers, API keys, tokens, cookies, passwords, bearer strings,
and full message/prompt body content in diagnostic logs.

## Codex Integration

Default mode:

```shell
subagent-router init
```

Default mode writes:

- `~/.codex/SUBAGENT_ROUTER_INSTRUCTIONS.md`
- `Follow instructions in ~/.codex/SUBAGENT_ROUTER_INSTRUCTIONS.md` at the top of `~/.codex/AGENTS.md`
- `~/.codex/agents/subagent-router-worker.toml`
- `~/.codex/agents/subagent-router-reviewer.toml`
- a managed `subagent_router` provider block in `~/.codex/config.toml`

## Init Upgrade Behavior

`subagent-router init` tracks installed files in `.subagent-router-manifest.json`
under the selected `--codex-home` directory.

- New package version: files that still match the previously installed content
  are updated to the new templates.
- User-customized files are preserved unless `--force` is passed.
- Legacy installs without a manifest are updated only when existing content
  matches a known managed template hash.
- Same package version: `subagent-router init` is idempotent.
- `config.toml`: the marked `>>> subagent-router >>>` block is replaced
  when markers are present. Content outside the markers is never touched.

## Smoke Checks

```shell
subagent-router doctor --mock
subagent-router start --mock --port 8787
curl -sS http://127.0.0.1:8787/health
```

Minimal mock request:

```shell
curl -sS http://127.0.0.1:8787/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-chat","stream":false,"input":"Say hello","tools":[]}'
```

## Accessing Runtime Diagnostics

```shell
# Activity summary (in-memory, resets on restart)
curl -sS http://127.0.0.1:8787/debug/activity

# Resolved state and log paths
curl -sS http://127.0.0.1:8787/debug/paths
subagent-router paths
```

---

## Provider Configuration

`SUBAGENT_ROUTER_PROVIDER` or `[defaults].provider` selects the active backend.
Provider settings can come from environment variables, `~/.config/subagent-router/config.toml`,
or project-local `.subagent-router.toml`.

| Provider Type | Env / Config Value | Status |
|---|---|---|
| DeepSeek | `deepseek` | Implemented (current default) |
| OpenAI-compatible | `openai-compatible` | Implemented |
| Ollama | `ollama` | Implemented |

Example config:

```toml
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
base_url = "https://my-endpoint.example.com/v1"
api_key = "sk-example"
model = "my-model"

[providers.ollama]
type = "ollama"
kind = "local"
enabled = true
base_url = "http://localhost:11434"
model = "llama3.1"
# Provider-specific overrides
max_cost_per_task = 0.00
max_tokens_per_task = 128000

[routes.cheap-review]
provider = "deepseek"
fallback_providers = ["ollama"]

[budgets]
max_cost_per_task = 0.05
max_cost_per_day = 2.00
max_tokens_per_session = 1000000
mode = "hard-stop"

[security]
provider_allowlist = ["deepseek", "ollama", "groq"]
provider_denylist = []
```

### Adding a Custom Provider

You can add any OpenAI-compatible backend (like Groq, vLLM, Together AI, CommandCode, etc.) by creating a new `[providers.<name>]` block and setting `type = "openai-compatible"`.

```toml
[providers.commandcode]
type = "openai-compatible"
kind = "cloud"
base_url = "https://api.commandcode.example.com/v1"
api_key_env = "COMMANDCODE_API_KEY" # Tells the router which env var holds the key
model = "commandcode-default-model" # Optional default model
```

Then, export the `COMMANDCODE_API_KEY` in your environment and either set `SUBAGENT_ROUTER_PROVIDER=commandcode` or explicitly use `?provider=commandcode` in your requests.

### Provider-Specific Overrides

You can override model and budget settings for a specific provider block. This is useful for pinning a model for a specific backend or setting more restrictive limits for expensive providers.

```toml
[providers.expensive-cloud]
type = "openai-compatible"
model = "gpt-4o"
max_cost_per_task = 0.50
```

Config precedence:

1. Command-line flags
2. Environment variables
3. Per-project config (`.subagent-router.toml`)
4. User-global config (`~/.config/subagent-router/config.toml`)
5. Built-in defaults

## Routing Policies

Deterministic routing policies select a provider and optional model from
request metadata, headers, query parameters, config file routes, and built-in
policy names.

Built-in policies:

- `safe-default` — balanced cost/capability for general tasks
- `cheap-review` — lowest-cost provider for review tasks
- `local-only` — restrict to local/Ollama models (privacy-sensitive tasks)
- `high-context` — provider with the largest context window
- `fast-draft` — lowest-latency provider for quick drafts
- `budget-capped` — honor per-task budget limits

Policy inputs will include task type, role, estimated context size, provider
capability metadata, token/cost budgets, privacy sensitivity, and user override.

Manual provider override lets the user pin a provider for a single task or
session:

```shell
SUBAGENT_ROUTER_PROVIDER=ollama subagent-router run -- codex
```

Fallbacks let a failed provider attempt fall through to a configured
alternative before returning an error.

## Usage and Audit Logs

Persistent, structured JSONL logs recording per-request metadata:

- provider and model selected
- routing policy and selection reason
- prompt / completion / total tokens (provider-reported or estimated)
- per-request estimated cost
- latency, retry count, fallback count
- success/failure status
- timestamps and trace IDs

Commands:

```shell
subagent-router usage
subagent-router usage --json
subagent-router logs --audit
```

## Budget Controls

Configurable limits that can stop or warn before exceeding a cost or token
threshold:

- Max cost per task
- Max tokens per task
- Warn-only mode vs. hard-stop mode

```shell
SUBAGENT_ROUTER_MAX_COST_PER_TASK=0.50
SUBAGENT_ROUTER_MAX_TOKENS_PER_TASK=100000
SUBAGENT_ROUTER_BUDGET_MODE=hard-stop
```

## Debug Bundle

A single-file or archive export for debugging routing and provider behavior
without exposing secrets:

```shell
subagent-router debug-bundle          # write archive to cwd
subagent-router debug-bundle --output /tmp/report.tar.gz
```

The bundle includes existing activity logs, session mirrors, audit logs, usage
summary, provider error diagnostics, and server logs.

## Stdin/Stdout Task Handoff

A generic handoff mechanism for primary agents that cannot use an HTTP or
provider-style integration directly. The router reads one Responses JSON
payload from stdin and writes one Responses JSON result to stdout.

```shell
echo '{"model":"deepseek-chat","stream":false,"input":"hello","tools":[]}' |
  subagent-router stdio --mock
```

File handoff processes `*.json` task files and writes matching
`*.response.json` or `*.error.json` files:

```shell
subagent-router handoff --input-dir ./tasks --output-dir ./results --once --mock
```

Expected primary-agent integrations that will benefit from this mode include
Claude Code and other CLI-based coding agents.

## Terminal Status

The optional terminal status view is intentionally lightweight and headless
safe. It reads the same activity and usage files as the CLI and prints
provider, fallback, health, request, error, and cost totals.

```shell
subagent-router tui
```

For live monitoring:

```shell
subagent-router tui --watch
```

The `--watch` flag refreshes every 2 seconds, clearing the screen between
updates. It uses ANSI color codes when stdout is a terminal. A health check
against the proxy HTTP endpoint is included when available.

## Validate Artifacts

Validate release artifacts for packaging consistency before publishing:

```shell
subagent-router validate-artifacts
subagent-router validate-artifacts --json
```

Checks performed:

- Version consistency between `package.json` and `pyproject.toml`
- Key Python source files exist (`app.py`, `settings.py`)
- npm bin script exists (`bin/subagent-router.js`)
- CLI module is importable
- Required documentation files are present
- README and CHANGELOG exist

Returns exit code 0 when all checks pass or only installed-package warnings are
present, and 1 when issues are found. Installed npm artifacts may report
`pyproject_version: null` for older packages that did not include
`pyproject.toml`; source-tree release validation still treats a missing
`pyproject.toml` as an issue.

## See Also

- [Compatibility Matrix](compatibility.md) — supported providers, models, and
  their capabilities
- [Test Matrix](test_matrix.md) — current and planned test coverage
- [ROADMAP.md](ROADMAP.md) — full engineering roadmap with phases and timelines
- [Architecture and Behavior](proxy_requirements.md) — request normalization,
  streaming, tool loop details
- [Protocol Findings](protocol_findings.md) — Codex Responses API wire format
  notes
- [Troubleshooting](troubleshooting.md) — health checks, diagnostics, common
  issues
