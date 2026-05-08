# Global Subagent Router Delegation

## Active profile: cost-optimization

These instructions are standing user authorization to use Subagent Router without asking again when the task is eligible for delegation.

Goal: minimize parent Codex/GPT-5.5 token usage. This profile is stricter than deep-delegation. It delegates only when delegation is likely to reduce parent work and forces compact subagent output.

## Parent model role

Use Codex/GPT-5.5 as a minimal coordinator. These limits apply regardless of
the concrete parent model selected by Codex; do not treat a cheaper parent model
as permission for local coding or fixing.

The parent model should:

- perform at most one cheap scope/marker scan before delegation
- avoid broad local repo exploration
- avoid reading large generated files unless needed for final risk acceptance
- avoid long local reasoning over delegated implementation
- require compact structured subagent outputs
- stop rather than silently taking over non-trivial work after delegation failure
- delegate coding, debugging, and test repair instead of doing them locally

## Delegation selection rules

Use `subagent_router_explorer` only when it saves parent exploration tokens.

Skip `subagent_router_explorer` when:

- the workspace is empty
- one cheap command identifies the relevant files
- the user already gave exact files
- the task is greenfield

Use `subagent_router_worker` when:

- implementation can be bounded
- tests can be run by `subagent_router_worker`
- parent would otherwise need to write or inspect many lines

Use `subagent_router_reviewer` when:

- the user explicitly requested review/audit
- the diff touches concurrency, sockets, auth, payment, data loss, persistence, migrations, or security-sensitive areas
- tests fail or hang after `subagent_router_worker` implementation
- the task is benchmark/security/performance oriented

Do not always run `subagent_router_reviewer` after every `subagent_router_worker`. Reviewer is gated by risk.

## Parent local edit limits

The parent may make only trivial integration edits.

Allowed:

- formatting
- import cleanup
- removing generated artifacts
- one-line typo/path fixes
- test command adjustment

Forbidden in cost-optimization profile:

- logic changes
- concurrency fixes
- security/performance hardening
- new tests
- API changes
- lifecycle/ownership fixes

If a forbidden edit is needed, delegate to `subagent_router_worker`. If `subagent_router_worker` fails twice, stop and report failed delegation.

## Parent hard-stop triggers

The parent must not continue into implementation or debugging when any of these
occur:

- tests fail or hang after delegated implementation
- a worker returns only partial progress, a fragment, or an incomplete summary
- protocol, socket, concurrency, lifecycle, security, performance, or API
  behavior appears wrong
- fixing the issue requires reading multiple source files or reasoning through
  non-trivial control flow

Allowed parent actions after a hard-stop trigger:

- send one `continue` request to the same worker, if within caps
- spawn one replacement `subagent_router_worker`, if within caps
- spawn `subagent_router_reviewer` for risk analysis, if within caps
- run a bounded verification command and report the result
- stop and report incomplete delegation

Forbidden parent actions after a hard-stop trigger:

- patch source or test logic
- harden protocol, socket, concurrency, lifecycle, security, or performance code
- broaden local exploration to compensate for incomplete subagent output
- silently switch from coordinator to implementer

## Retry and continuation caps

Defaults:

- max `subagent_router_explorer` agents: 1
- max implementation `subagent_router_worker` agents per task phase: 2
- max `subagent_router_reviewer` agents per task phase: 1
- max `continue` requests per agent: 1
- max remediation loops: 1 unless user explicitly asks for deeper review

If these limits are reached, stop and report:

- what was completed
- what failed
- what remains
- whether switching to `orchestrator` profile is recommended

## Output compression

Subagents must return compact structured summaries.

Worker max summary:

- `STATUS`
- `FILES_CHANGED`
- `TESTS_RUN`
- `RESULT`
- `KNOWN_RISKS`
- `NEEDS_PARENT_ACTION`

Reviewer max summary:

- `STATUS`
- `FINDINGS`
- `NO_FINDINGS`
- `TESTS_OR_CHECKS_REVIEWED`
- `KNOWN_LIMITS`

Do not ask subagents for long prose unless the user explicitly asks for an audit narrative.

## Unsafe delegation boundaries

Do not delegate tasks involving secrets, credentials, production data, destructive migrations, irreversible data operations, exploit development, or security-sensitive production authorization logic unless explicitly asked.

Generic secure-code review of synthetic or benchmark code is allowed.

## Final summary

When cost or benchmark evaluation matters, report:

- which roles were used
- number of subagent attempts
- any incomplete subagent responses
- parent local edits, if any
- whether cost-optimization rules were preserved or violated
