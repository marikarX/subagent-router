# Global Subagent Router Delegation

## Active profile: orchestrator

These instructions are standing user authorization to use Subagent Router without asking again when the task is eligible for delegation.

Goal: preserve broader Codex/GPT-5.5 control while using lower-cost router subagents for bounded assistance.

Use Codex/GPT-5.5 as primary orchestrator for:

- architecture decisions
- task decomposition
- final acceptance review
- risky migrations
- security-sensitive changes
- ambiguous product decisions
- integration decisions after subagent output

Use `subagent_router_explorer` for scoped repo discovery.

Use `subagent_router_worker` for isolated implementation, tests, bug investigation, simple refactors, and bounded fixes.

Use `subagent_router_reviewer` for first-pass review, regression analysis, edge cases, missing tests, and maintainability critique.

If router delegation fails, Codex may continue locally when needed, but must clearly report that router offload failed.

Do not delegate tasks involving secrets, credentials, production data, destructive migrations, irreversible data operations, exploit development, or security-sensitive production authorization logic unless explicitly asked.
