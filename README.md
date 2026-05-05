# Subagent Router

Route subagent work from primary coding agents to local, low-cost, or cloud model backends.

Subagent Router lets primary coding agents delegate subagent tasks to alternate model backends such as **DeepSeek**, **Ollama**, **Groq**, and other OpenAI-compatible providers. It features a robust routing engine, fallback logic, and granular budget controls to ensure privacy, performance, and cost-efficiency.

The current Codex integration talks to this proxy through a local `/v1/responses` HTTP endpoint. The proxy normalizes requests to various backend formats, manages streaming SSE, and tracks usage across tasks, sessions, and days.

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

## Quick Start

Check your configuration:

```shell
subagent-router doctor
subagent-router paths
```

Install Codex integration files:

```shell
subagent-router init
```

Start the proxy:

```shell
# Foreground
DEEPSEEK_API_KEY=... subagent-router start

# Background
DEEPSEEK_API_KEY=... subagent-router start --background
subagent-router tui --watch
```

Run Codex with an ephemeral proxy:

```shell
DEEPSEEK_API_KEY=... subagent-router run -- codex
```

## Features

- **Multi-Provider Routing**: Seamlessly switch between DeepSeek, local Ollama, and OpenAI-compatible endpoints (Groq, vLLM, etc.).
- **Smart Fallbacks**: Automatically retry failed requests on alternative backends.
- **Budget Controls**: Hard-stop or warn based on token usage or dollar cost per-task, per-session, or per-day.
- **Observability**: Structured audit logs with deep token tracking (in/cache/out), real-time usage tracking, and a lightweight interactive Terminal UI (`tui`).
- **Protocol Flexibility**: First-class support for the Codex internal Responses protocol, while also transparently accepting standard OpenAI `/v1/chat/completions` `messages` payloads for drop-in compatibility with curl and standard libraries.

## Configuration

The router can be configured via environment variables or a `config.toml` file.

### Common Environment Variables

- `SUBAGENT_ROUTER_PROVIDER`: Default provider (`deepseek`, `ollama`, `openai-compatible`)
- `SUBAGENT_ROUTER_BUDGET_MODE`: `warn` (default) or `hard-stop`
- `SUBAGENT_ROUTER_MAX_COST_PER_DAY`: Maximum daily spend in USD
- `SUBAGENT_ROUTER_MAX_TOKENS_PER_SESSION`: Token budget for the current session

### Example Config (`config.toml`)

```toml
[providers.groq]
type = "openai-compatible"
base_url = "https://api.groq.com/openai/v1"
model = "llama-3.3-70b-versatile"

[budgets]
max_cost_per_task = 0.05
max_cost_per_day = 5.00
mode = "hard-stop"
```

More configuration details are in [docs/usage.md](docs/usage.md).

## Roadmap

See [docs/ROADMAP.md](docs/ROADMAP.md) for implemented and planned features including intelligent provider scoring and advanced routing policies.

## Documentation

- [Usage and configuration](docs/usage.md)
- [Architecture and behavior](docs/proxy_requirements.md)
- [Protocol notes](docs/protocol_findings.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Provider compatibility](docs/compatibility.md)
- [Test matrix](docs/test_matrix.md)
- [Release checklist](docs/release_checklist.md)
- [Changelog](CHANGELOG.md)

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
