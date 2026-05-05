# Subagent Router Roadmap

This roadmap describes the engineering path for Subagent Router: a provider-neutral router for delegated subagent work from primary coding agents to local, low-cost, or cloud model backends.

## Project Vision

Subagent Router lets primary coding agents delegate bounded subagent work without being coupled to one model vendor, one local runtime, or one agent client. It makes provider choice explicit, auditable, and configurable so users can trade off cost, latency, privacy, context size, and capability per task.

---

## Completed Phases ✅

### Phase 1: Provider Abstraction
- Defined a provider interface for request normalization, invocation, streaming, and error mapping.
- Implemented **DeepSeek**, **OpenAI-compatible** (Groq, vLLM), and **Ollama** providers.
- Added provider capability metadata (context window, tool support, cost hints).

### Phase 2: Local Model Support
- Full **Ollama** integration with local model discovery and zero-cost accounting.
- Model capability overrides for local backends.
- Fallback logic for unreachable local endpoints.

### Phase 3: Routing Policies
- Config-driven provider selection and fallback chains.
- Per-request manual overrides via headers, query params, and metadata.
- Configurable routes (e.g., `cheap-review`, `local-only`).

### Phase 4: Observability and Audit Logging
- Structured audit logs (`audit.jsonl`) recording provider selection, usage, and latency.
- Redaction of sensitive API keys and authorization headers in logs.
- `subagent-router debug-bundle` for secure troubleshooting.
- Compact request/response tracing.

### Phase 5: Usage and Cost Control
- Persistent tracking of tokens and USD cost across tasks, sessions, and days.
- **Daily Budget Limits**: Automatic enforcement of spend/token caps across proxy restarts.
- **Session Budget Limits**: Limits scoped to a single proxy process.
- **Hard-stop mode**: Returns `402 Payment Required` when budgets are exceeded.

### Phase 6: Terminal UI and UX
- Lightweight `subagent-router tui` for real-time status monitoring.
- `subagent-router doctor` for environment and configuration validation.
- `subagent-router run -- <cmd>` for ephemeral, secure proxy orchestration.
- `sar` short alias for all commands.
- [ ] **Interactive TUI Configuration**: Add ability to dynamically change policy settings and configurations directly from the TUI.
- [ ] **Main Agent Integration**: Setup Codex CLI and VSCode extension to natively use the router as the primary main agent, in addition to orchestrating subagents.

---

## Current Roadmap 🚀

### Phase 7: Multi-client Integrations
- [x] **Codex**: Full integration via `init` command and managed provider blocks.
- [x] **Stdin/Stdout**: Generic `stdio` mode for CLI-based agents.
- [x] **File-based Handoff**: `handoff` command for asynchronous task processing.
- [ ] **Claude Code**: Native integration for delegated work in Claude Code environments.
- [ ] **Antigravity**: Research and implement integration with Antigravity for delegated work in Antigravity environments.

### Phase 8: Advanced Routing
- [ ] **Dynamic Provider Scoring**: Automatically route based on real-time latency and reliability metrics.
- [ ] **Task-Type Specialization**: Automatic routing for specialized tasks like "large file analysis" vs "quick code edit".
- [ ] **Load Balancing**: Distribute requests across multiple identical provider endpoints.

### Phase 9: Security and safety model refinements
- [x] Redaction of secrets in logs and debug bundles.
- [x] Config file permissions validation (600/644).
- [ ] Encrypted secrets storage integration.
- [ ] Content-safety filtering for outgoing prompts.

---

## Provider Roadmap

| Provider | Status |
|---|---|
| DeepSeek | ✅ Stable |
| OpenAI-compatible (Groq, etc.) | ✅ Stable |
| Ollama (Local) | ✅ Stable |
| OpenRouter | 🔄 Planned |
| Anthropic | 🔄 Planned |
| Together AI | 🔄 Planned |

## Integration Roadmap

| Integration | Status |
|---|---|
| Codex | ✅ Stable |
| Generic Stdio | ✅ Stable |
| File Handoff | ✅ Stable |
| Claude Code | 🔄 Researching |
| Antigravity | 🔄 Researching |
| GitHub Copilot Extensions | 🔄 Planned |
