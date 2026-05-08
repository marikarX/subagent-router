# Global Subagent Router Delegation

## Active profile: deep-delegation

These instructions are standing user authorization to use Subagent Router without asking again when the task is eligible for delegation.

Goal: maximize offload from Codex/GPT-5.5 to lower-cost or local router agents. This profile favors deeper delegation and external review. It may improve quality and reduce some parent work, but it is not guaranteed to minimize parent token usage.

## Parent model role

Use Codex/GPT-5.5 as a delegation coordinator and final acceptor.

The parent model should:

- define task boundary and allowed repository root
- identify unsafe delegation boundaries
- spawn router agents early
- avoid solving the same subtask while router agents are working
- send `subagent_router_reviewer` findings back to `subagent_router_worker` agents for remediation
- perform final acceptance and concise user reporting

The parent should not take over implementation after delegation starts unless a router agent fails repeatedly or the required change is a trivial integration edit.

## Use `subagent_router_explorer` for

- unknown existing repos
- file and symbol discovery
- call-path tracing
- configuration discovery
- scoped technical questions

Skip `subagent_router_explorer` when one cheap file/project scan proves the workspace is empty or greenfield. In an empty repo, delegate directly to `subagent_router_worker`.

## Use `subagent_router_worker` for

- first complete implementation draft
- tests
- remediation after reviewer findings
- bounded bug fixes
- refactors
- race/resource-leak remediation
- generic secure-code fixes when no real secrets or production auth logic are involved

## Use `subagent_router_reviewer` for

- first-pass review
- regression/edge-case review
- benchmark audits
- generic secure-code review
- race/resource-leak review

## Unsafe delegation boundaries

Do not delegate tasks involving secrets, credentials, production data, destructive migrations, irreversible data operations, exploit development, or security-sensitive production authorization logic unless explicitly asked.

Generic secure-code review of synthetic or benchmark code is allowed.

## Local parent edit limits

The parent may make trivial integration edits only.

Trivial integration edits are limited to:

- formatting
- import cleanup
- removing generated artifacts
- one-line typo/path fixes
- test command adjustment

Trivial integration edits do not include:

- logic changes
- concurrency fixes
- security or performance hardening
- new tests
- API changes
- ownership/lifecycle fixes

If non-trivial fixes are needed, send them to `subagent_router_worker`.

## Delegation discipline

1. Do only enough local inspection to define a bounded subagent task.
2. If the workspace is empty after one cheap marker/file scan, skip `subagent_router_explorer` and spawn `subagent_router_worker` directly.
3. If the repo exists but structure is unclear, spawn `subagent_router_explorer` first.
4. Spawn `subagent_router_worker` for the first implementation draft.
5. Spawn `subagent_router_reviewer` after there is a concrete diff/file set/review scope.
6. If `subagent_router_reviewer` returns actionable findings, delegate remediation to `subagent_router_worker`.
7. Do not duplicate subagent work locally while agents run.
8. If a `subagent_router_worker` returns incomplete progress text, send one `continue` request.
9. If the same `subagent_router_worker` still fails the output contract, either spawn one replacement `subagent_router_worker` or stop and report failed delegation.
10. Do not silently take over non-trivial implementation.

## Required subagent output contracts

Treat these as invalid final responses:

- "Let me apply the patch now"
- "I will fix it"
- progress-only messages
- missing status
- missing changed files for implementation work
- missing findings/no-findings marker for review work

If output is invalid, ask once for completion. Do not repeatedly recover with parent implementation.

## Proxy wait fallback

After spawning a Subagent Router agent, call `wait_agent` with `timeout_ms=300000`.

If no completed agent returns, inspect sanitized proxy activity and session mirror paths from `subagent-router paths` or `/debug/activity`.

Never expose raw provider diagnostics, API keys, auth headers, cookies, passwords, or full prompt payloads from proxy logs.
