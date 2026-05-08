# Feature Document: Delegation Profiles and Router Agent Roles

## Purpose

Subagent Router supports delegation profiles so users can choose how much
control stays with Codex/GPT-5.5 versus how much work is offloaded to
lower-cost or local router agents.

The profile controls when and how aggressively the parent agent delegates. The
role templates control what each router-backed subagent should do.

## Canonical Profiles

- `cost-optimization` (default): best-effort minimization of parent
  Codex/GPT-5.5 token usage through compact subagent output, retry caps,
  strict local parent edit limits, hard-stop triggers, and selective
  delegation.
- `deep-delegation`: maximizes external delegation for exploration,
  implementation, review, and remediation. Useful for experiments and
  quality-through-delegation, but not guaranteed to minimize parent tokens.
- `orchestrator`: preserves broader Codex/GPT-5.5 control while using router
  agents as bounded helpers.
- `manual`: installs provider config and role files without global automatic
  delegation.

Cost optimization is best-effort and measured through reduced parent Codex
token usage, not wall-clock time. The parent local-work limits are
model-agnostic; selecting a cheaper parent model does not permit local coding,
debugging, test repair, or protocol/concurrency/lifecycle fixes.

## Aliases

Profile aliases resolve to canonical profile names:

- `quality`, `conservative`, `codex-control` -> `orchestrator`
- `deep`, `deep-delegate`, `aggressive`, `aggressive-delegation` ->
  `deep-delegation`
- `cost`, `cost-optimized`, `budget`, `budget-optimized`, `token-saving`,
  `token-optimized` -> `cost-optimization`
- `opt-in`, `provider-only` -> `manual`

Invalid profiles fail clearly.

## CLI Behavior

```shell
subagent-router init
subagent-router init --profile cost-optimization
subagent-router init --profile deep-delegation
subagent-router init --profile orchestrator
subagent-router init --profile manual
```

`subagent-router init` is equivalent to
`subagent-router init --profile cost-optimization`.

Compatibility modes remain:

- `--mode default`: installs global profile instructions using `--profile`.
- `--mode opt-in`: installs `$deepseek`, `/deepseek`, provider config, and role
  files without global instructions.
- `--mode provider-only`: installs provider config and role files only.

When `--profile` is supplied with `opt-in` or `provider-only`, the command keeps
existing behavior and prints a warning that the profile is ignored.

## Role Templates

- `subagent_router_explorer`: read-only repo discovery, file mapping,
  call-path tracing, and scoped technical questions.
- `subagent_router_worker`: delegated repo inspection, first-pass
  implementation, refactors, tests, documentation updates, and bounded bug
  fixes.
- `subagent_router_reviewer`: first-pass code review, regression analysis,
  edge cases, benchmark audits, and implementation critique.

The templates are Subagent Router role templates, not Codex built-ins.
