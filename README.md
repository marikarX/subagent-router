# Subagent Router

Route subagent work from primary coding agents to local, low-cost, or cloud model backends.

Subagent Router lets primary coding agents delegate subagent tasks to alternate model backends such as DeepSeek, Ollama, OpenRouter, and other OpenAI-compatible providers.

The current Codex integration talks to this proxy through a local
`/v1/responses` HTTP endpoint. The current DeepSeek backend converts Codex
Responses requests to DeepSeek chat/completions requests, then converts
DeepSeek responses back to Responses JSON or SSE. The primary coding agent
remains responsible for executing tools.

## Install

From npm:

```shell
npm install -g subagent-router
```

From source:

```shell
cd subagent-router
python -m venv .venv
. .venv/bin/activate
pip install -e '.[server]'
```

For local npm package testing from this checkout:

```shell
npm pack
npm install -g ./subagent-router-0.1.0.tgz
```

## Quick Start

Check the CLI:

```shell
subagent-router --help
subagent-router paths
```

Install Codex integration files:

```shell
subagent-router init
```

Default init mode writes the full Subagent Router delegation instructions to
`~/.codex/SUBAGENT_ROUTER_INSTRUCTIONS.md` and adds only that file path as the first line of
`~/.codex/AGENTS.md`. Existing `AGENTS.md` content stays below it.

Start the proxy:

```shell
DEEPSEEK_API_KEY=... subagent-router start
```

For deterministic local smoke tests without a DeepSeek key:

```shell
subagent-router start --mock
```

Run Codex with an ephemeral proxy:

```shell
DEEPSEEK_API_KEY=... subagent-router run -- codex
```

`subagent-router run --` starts the proxy on a free loopback port, injects temporary Codex
provider config with `-c`, runs the requested command, and stops the proxy when
the command exits.

## Commands

```shell
subagent-router init
subagent-router start
subagent-router run -- codex ...
subagent-router doctor
subagent-router paths
subagent-router install-service
```

`sar` is also installed as a short alias for `subagent-router`.

## Configuration

Minimum real-provider configuration:

```shell
export DEEPSEEK_API_KEY=...
```

Common options:

- `DEEPSEEK_BASE_URL`: defaults to `https://api.deepseek.com/v1`
- `DEEPSEEK_MODEL`: optional upstream model override
- `SUBAGENT_ROUTER_HOST`: defaults to `127.0.0.1`
- `SUBAGENT_ROUTER_PORT`: defaults to `8787`
- `SUBAGENT_ROUTER_STATE_DIR`: defaults to `$XDG_STATE_HOME/subagent-router`
  or `~/.local/state/subagent-router`

More configuration details are in [docs/usage.md](docs/usage.md).

## Roadmap

See [docs/ROADMAP.md](docs/ROADMAP.md) for the planned provider abstraction,
local model support, routing policies, usage tracking, cost controls,
observability, optional terminal UI, and future primary-agent integrations.

## Documentation

- [Usage and configuration](docs/usage.md)
- [Architecture and behavior](docs/proxy_requirements.md)
- [Protocol notes](docs/protocol_findings.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Test matrix](docs/test_matrix.md)

## Development

```shell
uv run pytest
```

Run a mock proxy for local checks:

```shell
subagent-router start --mock --port 8787
curl -sS http://127.0.0.1:8787/health
curl -sS http://127.0.0.1:8787/debug/activity
```

## License

MIT. See [LICENSE](LICENSE).
