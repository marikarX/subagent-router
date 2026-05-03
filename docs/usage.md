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

The npm package exposes both `subagent-router` and the short `sar` alias.

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

## Running

Run with a real DeepSeek key:

```shell
DEEPSEEK_API_KEY=... subagent-router start
```

Run with deterministic mock responses:

```shell
subagent-router start --mock
```

Run in the background:

```shell
DEEPSEEK_API_KEY=... subagent-router start --background
```

The background process writes its PID to
`$SUBAGENT_ROUTER_STATE_DIR/subagent-router.pid` and server output to
`$SUBAGENT_ROUTER_STATE_DIR/logs/server.log`.

Lifecycle commands:

```shell
subagent-router status
subagent-router logs --follow
subagent-router restart
subagent-router stop
```

To start the proxy and immediately attach the terminal to its logs:

```shell
DEEPSEEK_API_KEY=... subagent-router start --background --attach-logs
```

Run one command with an ephemeral proxy:

```shell
DEEPSEEK_API_KEY=... subagent-router run -- codex
```

`run --` starts the proxy on a free loopback port when `--port` is omitted,
injects temporary Codex provider overrides with `-c`, runs the requested
command, and stops the proxy when the command exits.

## Environment

- `DEEPSEEK_API_KEY`: required unless mock mode is enabled
- `DEEPSEEK_BASE_URL`: defaults to `https://api.deepseek.com/v1`
- `DEEPSEEK_MODEL`: optional upstream model override
- `DEEPSEEK_SEND_PARALLEL_TOOL_CALLS=1`: forwards `parallel_tool_calls`
- `DEEPSEEK_PROXY_MOCK=1`: returns deterministic local responses
- `DEEPSEEK_ALLOW_APPLY_PATCH=1`: forwards `apply_patch` for non-worker
  requests
- `SUBAGENT_ROUTER_HOST`: defaults to `127.0.0.1`
- `SUBAGENT_ROUTER_PORT`: defaults to `8787`
- `SUBAGENT_ROUTER_STATE_DIR`: defaults to
  `$XDG_STATE_HOME/subagent-router`, or
  `~/.local/state/subagent-router` when `XDG_STATE_HOME` is unset
- `SUBAGENT_ROUTER_LOG_DIR`: defaults to `logs/client_payloads` under
  `SUBAGENT_ROUTER_STATE_DIR`
- `SUBAGENT_ROUTER_ACTIVITY_FILE`: defaults to `logs/activity.json` under
  `SUBAGENT_ROUTER_STATE_DIR`
- `SUBAGENT_ROUTER_SESSION_MIRROR_FILE`: defaults to
  `logs/session_mirror.json` under `SUBAGENT_ROUTER_STATE_DIR`
- `SUBAGENT_ROUTER_PROVIDER_ERROR_LOG_DIR`: defaults to
  `logs/provider_errors` under `SUBAGENT_ROUTER_STATE_DIR`
- `SUBAGENT_ROUTER_TRACE=1`: prints compact request/response trace lines

The older `CODEX_PROXY_*` names are still accepted as compatibility aliases for
the Codex integration.

Relative path overrides resolve under `SUBAGENT_ROUTER_STATE_DIR`. Absolute
paths and `~`-prefixed paths are expanded once at startup. The current resolved
paths are exposed by:

```shell
subagent-router paths
curl -sS http://127.0.0.1:8787/debug/activity
curl -sS http://127.0.0.1:8787/debug/paths
```

## Upgrade Behavior

`subagent-router init` tracks installed files in `.subagent-router-manifest.json` under the selected
`--codex-home` directory.

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
