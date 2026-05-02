# Subagent Router Roadmap

This roadmap describes the practical engineering path for Subagent Router: a
provider-neutral router for delegated subagent work from primary coding agents
to local, low-cost, or cloud model backends.

## Project Vision

Subagent Router should let a primary coding agent delegate bounded subagent
work without being coupled to one model vendor, one local runtime, or one
agent client. The product should make provider choice explicit, auditable, and
configurable so users can trade off cost, latency, privacy, context size, and
capability per task.

Codex plus DeepSeek is the first working path. It proves the routing shape, but
neither Codex nor DeepSeek defines the whole product. Future integrations
should include Claude Code and other coding-agent clients. Future providers
should include local models, provider aggregators, cloud APIs, and generic
OpenAI-compatible endpoints.

## Current Baseline

### Completed baseline

- Codex is treated as the first primary-agent integration.
- DeepSeek is treated as the first provider/backend.
- Product-level docs and package naming are provider-neutral.
- The repo is ready to evolve toward multiple integrations and multiple
  providers.

## Roadmap Phases

### Phase 1: Provider abstraction

- Define a provider interface for request normalization, invocation, streaming,
  response normalization, error mapping, and usage extraction.
- Treat DeepSeek as one provider implementation behind that interface.
- Add generic OpenAI-compatible provider support with configurable base URL,
  API key, and model names.
- Add config-driven provider selection.
- Add provider capability metadata:
  - context window
  - tool support
  - streaming support
  - cost hints
  - pricing metadata
  - reasoning support
  - token accounting support
  - usage reporting support
  - cache/reasoning token reporting support
  - timeout/retry behavior
- Keep provider-specific logic isolated from product-level routing logic.
- Make it straightforward to add new provider adapters without changing the
  primary-agent integration layer.

### Phase 2: Local model support

- Add an Ollama provider.
- Support local endpoint configuration.
- Add model capability overrides for local models whose metadata is incomplete
  or unavailable.
- Document expected limitations for local models, including context limits,
  tool-call fidelity, latency, and response quality variance.
- Add fallback behavior when the local model endpoint or selected model is
  unavailable.
- Track local model usage separately from paid API usage.
- Treat local model API cost as zero while still tracking token estimates,
  latency, request count, and failures.

### Phase 3: Routing policies

Add routing based on:

- task type
- expected complexity
- cost target
- token budget
- privacy sensitivity
- model capability
- timeout budget
- historical provider reliability
- historical model latency
- manual override

Initial policy examples:

- `cheap-review`
- `local-only`
- `high-context`
- `fast-draft`
- `safe-default`
- `budget-capped`

Routing should support clear, config-driven rules first. Avoid overengineering
automatic model selection until usage data and failure modes are better
understood.

### Phase 4: Multi-client integrations

- Keep the Codex integration.
- Add Claude Code integration if technically feasible.
- Support generic stdin/stdout task handoff.
- Support file-based task handoff for clients that cannot use an HTTP or
  provider-style integration directly.
- Avoid hard-coding assumptions from one primary agent.
- Separate client/integration concerns from provider/backend concerns.
- Document integration-specific constraints, required setup, and known
  limitations.

### Phase 5: Token usage, utilization, and cost control

Add first-class usage tracking because cost control is a core reason for the
project.

Track token usage:

- prompt tokens
- completion tokens
- total tokens
- cached tokens if provider reports them
- reasoning tokens if provider reports them
- estimated tokens when provider does not report usage
- model/provider token accounting differences

Track cost:

- per-request estimated cost
- per-provider cost table
- per-model cost table
- daily/session/project totals
- local model usage as zero API cost but still tracked for utilization
- unknown/estimated cost when provider pricing is unavailable

Track utilization:

- requests per provider
- requests per model
- average latency
- timeout rate
- retry count
- fallback count
- success/failure rate
- average tokens per task type
- average cost per task type

Add budget controls:

- max cost per task
- max tokens per task
- max daily/session spend
- warn-only mode
- hard-stop mode
- per-provider limits
- per-model limits

Add reporting/export support:

- JSONL usage logs
- summary report command
- per-session usage summary
- per-project usage summary
- debug bundle with sensitive fields redacted

### Phase 6: Observability and audit logging

- Add structured logs.
- Record request/response metadata.
- Add per-task trace IDs.
- Record provider selection reason.
- Record routing policy used.
- Record retry/fallback chain.
- Add redaction support.
- Add dry-run mode.
- Add a debug bundle for troubleshooting.
- Clearly distinguish between:
  - raw provider responses
  - normalized provider responses
  - final response returned to the primary agent
- Log enough metadata to troubleshoot routing and provider behavior without
  leaking secrets or sensitive prompt content by default.

### Phase 7: Security and safety model

- Keep API keys and provider secrets out of logs.
- Redact sensitive fields in debug bundles.
- Support config file permissions checks.
- Support environment-variable-based secrets.
- Avoid writing full prompts/responses to persistent logs unless explicitly
  configured.
- Add clear warning for unsafe debug modes.
- Add provider allowlist/denylist support.
- Add local-only routing mode for sensitive tasks.
- Document privacy implications of sending code/context to cloud providers.

### Phase 8: Configuration and secrets management

- Define a stable config file schema.
- Support provider configuration blocks.
- Support model aliases.
- Support routing policy configuration.
- Support environment variable overrides.
- Support per-project config.
- Support user-global config.
- Add config validation command.
- Add clear error messages for missing API keys, invalid provider names,
  unsupported models, and malformed config.

Suggested config concepts:

- providers
- models
- routes
- policies
- budgets
- logging
- redaction
- defaults

### Phase 9: Testing and compatibility matrix

- Add integration tests across providers.
- Add contract tests for provider adapters.
- Add failure-mode tests:
  - provider timeout
  - invalid API key
  - malformed response
  - truncated output
  - unsupported capability
  - missing usage metadata
  - provider returns usage in unexpected format
  - fallback provider unavailable
- Add CLI tests.
- Add config validation tests.
- Check documentation examples with tests where practical.
- Maintain a compatibility matrix for:
  - primary-agent integrations
  - provider adapters
  - model capabilities
  - supported operating systems
  - Python/Node versions if applicable

### Phase 10: Packaging and distribution

- Stabilize package metadata.
- Ensure clean install from source.
- Ensure CLI entry points work after install.
- Add a release checklist.
- Add a versioning policy.
- Add a changelog.
- Add a CI workflow.
- Add a minimal install verification command.
- Document upgrade path for config and manifest files.
- Avoid publishing stale generated files, caches, logs, or build artifacts.

### Phase 11: Terminal UI

Add an optional TUI for inspecting and controlling Subagent Router during local
development.

The TUI should support:

- recent task history
- active task status if long-running tasks are supported
- provider and model used per task
- routing policy used per task
- provider selection reason
- token usage per task
- estimated cost per task
- latency per task
- retry and fallback chain
- success/failure status
- config validation status
- provider health checks
- budget usage summary
- usage summaries by provider/model/session/project
- log viewer with redaction
- debug bundle generation

The TUI should be optional. The CLI and config files must remain the primary
automation interface.

Do not make the TUI required for headless use, CI, or agent-driven workflows.

## Provider Roadmap

### Current provider

- DeepSeek

### Planned providers

- Generic OpenAI-compatible endpoint
- Ollama
- OpenRouter
- Groq
- Together
- Anthropic-compatible/cloud APIs where appropriate

Each provider adapter should document and test the following concerns:

- authentication
- base URL configuration
- model naming
- token usage reporting
- streaming support
- context window metadata
- timeout/retry behavior
- pricing metadata
- fallback compatibility
- health check behavior

## Primary-Agent Integration Roadmap

### Current integration

- Codex

### Planned integrations

- Claude Code
- Generic CLI/stdin/stdout handoff
- File-based task handoff
- Other coding-agent clients if their extension points support it

Each integration should document:

- how tasks are received
- how results are returned
- what assumptions the integration makes
- how provider routing is selected
- where logs and usage records are written
- how failures are surfaced back to the primary agent

## Routing Policy Roadmap

Routing should start as explicit configuration, not opaque automation. The
first useful implementation is a deterministic rules engine that maps task
attributes and manual overrides to provider/model choices.

Expected policy inputs:

- task type and role
- estimated context size
- provider/model capability metadata
- token and cost budgets
- privacy sensitivity
- timeout budget
- historical latency and failure rate
- user or project override

Expected policy outputs:

- selected provider and model
- fallback chain
- budget behavior
- provider selection reason for logs and user-facing diagnostics

## Token Usage, Utilization, and Cost Control

Usage and cost data should be captured close to the provider boundary, then
normalized into product-level records. Provider-reported usage should be stored
when available. Estimated usage should be clearly marked when a provider does
not report complete token data.

Local models should count toward utilization even when their API cost is zero.
That lets users compare local latency and failure rates against paid providers
without treating local usage as invisible.

## Security and Safety Model

The safe default is metadata-rich logging without persistent prompt or response
body storage. Full prompt/response logging should require explicit opt-in and a
clear warning.

Security-sensitive tasks should be routable to `local-only` policies. Cloud
providers should be easy to disable at provider, project, and policy level.

## Observability and Audit Logging

Observability should answer practical questions:

- Which provider and model handled this task?
- Why was that provider selected?
- How long did it take?
- How many tokens were used or estimated?
- What did it cost or likely cost?
- Did retries or fallbacks happen?
- What normalized response was returned to the primary agent?

Audit logs should preserve enough metadata to debug routing and provider
behavior while redacting secrets and avoiding sensitive prompt content by
default.

## Configuration and Secrets Management

Configuration should support simple environment-variable setup for the current
DeepSeek path while evolving toward a stable schema for multi-provider routing.

Config precedence should be explicit:

1. command-line flags
2. environment variables
3. per-project config
4. user-global config
5. built-in defaults

Secrets should be supplied through environment variables or secret backends,
not plain logged config dumps.

## Testing and Compatibility Matrix

Provider contract tests should make every adapter prove the same basic
behaviors: request normalization, streaming and non-streaming response handling,
usage extraction, error mapping, timeout behavior, and fallback compatibility.

The compatibility matrix should be treated as release documentation. It should
show which primary-agent integrations, provider adapters, models, operating
systems, and runtime versions are expected to work.

## Packaging and Distribution

Distribution should stay boring and verifiable. A user should be able to
install the package, run a minimal verification command, validate config, and
start the router without manual file surgery.

Release artifacts should exclude generated caches, logs, local state, and
debug bundles.

## Terminal UI

The TUI is a developer convenience, not a required runtime. It should read from
the same logs, config, and health endpoints used by CLI workflows. Any TUI-only
control should have an equivalent CLI or config-file path before it is treated
as core functionality.
