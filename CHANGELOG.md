# Changelog

## Unreleased

## [0.2.2] - 2026-05-05

### Added

- Added cached-input pricing support to provider capabilities, provider
  configuration, runtime config persistence, and `/v1/config` responses.
- Added per-model provider pricing overrides and provider-level worker and
  reviewer model assignments.
- Added runtime config updates for provider pricing, provider roles, nullable
  provider model overrides, and selectively persisted routing policy changes.
- Added a `POST /v1/reset` endpoint that clears in-memory activity state and
  removes usage, usage JSONL, and audit log files.
- Added TUI support for arrow and page-key navigation, remote reset calls,
  provider role display, cached-input pricing, and model-specific pricing
  overrides.

### Changed

- Bumped Python and npm package versions to `0.2.2`.
- Updated usage normalization to parse cached-token and reasoning-token counts
  from provider responses instead of reporting those fields as zero.
- Updated usage summaries to track total input, cached input, and output tokens
  in global and daily usage records.
- Updated subagent model detection to derive supported aliases from the shared
  model alias table.
- Updated activity and audit diagnostics to retain final session events across
  mirror truncation and include selected provider/model context on failures.

### Fixed

- Fixed request normalization for system or developer messages that appear
  between a tool call and its tool output by delaying those messages until after
  the tool output.
- Fixed runtime config serialization so provider entries include pricing, role
  assignments, and model pricing while omitting null pricing values.
- Fixed TUI model switching so provider-specific worker and reviewer model
  choices are saved as provider role settings and global routing model
  overrides are cleared.

## [0.2.1] - 2026-05-05

### Added

- Added a provider abstraction layer with adapters for DeepSeek, generic
  OpenAI-compatible `/chat/completions` endpoints, and local Ollama models.
- Added provider capability metadata for context windows, tool support,
  streaming support, reasoning support, token accounting, pricing hints,
  timeout settings, and retry metadata.
- Added config-file support for `~/.config/subagent-router/config.toml` and
  project-local `.subagent-router.toml`, including `[defaults]`,
  `[providers.*]`, `[routes.*]`, `[budgets]`, `[security]`, and
  `[predictions.*]` sections.
- Added provider selection from environment, config files, CLI flags, request
  query parameters, request headers, and request metadata.
- Added routing policies and built-in policy names: `safe-default`,
  `cheap-review`, `local-only`, `high-context`, `fast-draft`, and
  `budget-capped`.
- Added fallback provider chains for transport errors and upstream 5xx
  responses, with provider health tracking and fallback chain diagnostics.
- Added automatic provider ordering from provider predictions and observed
  provider health when automatic routing is enabled.
- Added provider allowlist and denylist controls.
- Added local-provider accounting so Ollama requests report zero API cost while
  still tracking latency, tokens, request counts, and failures.
- Added per-task, per-provider, per-model, per-session, and per-day budget
  controls for cost and tokens, including `max_spend_*` aliases and warn vs.
  hard-stop modes.
- Added dry-run provider responses for exercising routing, normalization, and
  metadata without calling an upstream provider.
- Added persistent usage summaries, bounded recent usage records, usage JSONL,
  and structured audit JSONL logs.
- Added success and failure audit records with provider, model, routing policy,
  selection reason, latency, token usage, estimated cost, and fallback chain
  details.
- Added `/debug/config` and expanded `/health`, `/debug/activity`, and
  `/debug/paths` diagnostics with sanitized provider, budget, routing, usage,
  and provider-health data.
- Added `subagent-router usage`, `logs --audit`, `debug-bundle`, `stdio`,
  `handoff`, `tui`, and `validate-artifacts` commands.
- Added common CLI flags for provider selection, fallback providers,
  OpenAI-compatible endpoints, Ollama settings, budget mode, provider security
  lists, dry-run, audit logs, and usage files.
- Added stdin/stdout and file-based task handoff modes for generic primary-agent
  integrations.
- Added a compact terminal status view with optional watch mode for health,
  provider, activity, and usage totals.
- Added release artifact validation for package/version consistency, required
  files, docs, npm bin, changelog, and CLI importability.
- Added a GitHub Actions CI workflow that installs test dependencies, checks
  whitespace, and runs `pytest -q`.
- Added provider compatibility documentation and a release checklist.

### Changed

- Bumped Python and npm package versions to `0.2.1` and aligned
  `pyproject.toml` with `package.json`.
- Expanded `README.md`, usage docs, troubleshooting docs, and the router
  instruction template to describe implemented provider routing, diagnostics,
  compatibility, release checks, and subagent wait fallback behavior.
- Updated `subagent-router doctor` to validate the selected provider and
  fallback providers instead of checking only the DeepSeek API key.
- Updated `subagent-router paths` output to include provider summaries and the
  new audit and usage paths.
- Updated mock/provider code so deterministic mock DeepSeek responses are owned
  by the DeepSeek provider adapter.
- Updated response metadata to include selected provider, provider kind,
  provider model, routing policy, and provider selection reason.
- Updated provider error diagnostics to include provider/model context and omit
  raw tool-call arguments from sanitized diagnostic output.
- Updated packaging rules to exclude Python caches and egg-info from npm
  package contents.
- Updated `.gitignore` to exclude local helper scripts matching
  `scripts/local-*.sh`.
- Added full `usage` payloads to internal audit records to support detailed
  token tracking.
- Enhanced the `Recent Requests` TUI panel by adding a `Toks (in/cache/out)` 
  column and a `Model` column for improved visibility into provider performance
  and cache hit rates.

### Fixed

- Reject empty provider outputs and empty final assistant messages instead of
  returning empty successful responses.
- Retry incomplete subagent outputs once and synthesize a continuation tool call
  when a write-capable subagent returns progress text or a final answer without
  evidence of the requested write.
- Preserve `apply_patch` for write-capable worker aliases by default while
  still dropping it for read-only reviewer requests.
- Preserve and replay provider `reasoning_content` internally for continuation
  correctness while keeping it out of Responses output and trace logs.
- Removed a dead duplicate `mock_deepseek_response` implementation from
  `app.py`; the provider adapter implementation is the single active mock path.
- Fixed standard OpenAI `messages` compatibility in the `/v1/chat/completions`
  endpoint by adding an automatic translation layer to the internal Codex `input`
  format, resolving "provider returned empty output" errors.
- Fixed an overly aggressive `SECRET_KEY_RE` redaction rule that incorrectly
  censored `total_tokens` and other metrics in the audit logs.
- Fixed a UI bug where successful requests displayed as `ERR` in the TUI due
  to a strict check for `"success"` instead of the router's internal `"ok"` state.
- Fixed a TUI rendering exception (`Audit log error`) caused by parsing legacy
  `[REDACTED]` strings in older audit logs.
