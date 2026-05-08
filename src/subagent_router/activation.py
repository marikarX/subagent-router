from __future__ import annotations

from importlib.resources import files


CANONICAL_PROFILES = (
    "cost-optimization",
    "deep-delegation",
    "orchestrator",
    "manual",
)
DEFAULT_PROFILE = "cost-optimization"
PROFILE_ALIASES = {
    "cost": "cost-optimization",
    "cost-optimization": "cost-optimization",
    "cost-optimized": "cost-optimization",
    "budget": "cost-optimization",
    "budget-optimized": "cost-optimization",
    "token-saving": "cost-optimization",
    "token-optimized": "cost-optimization",
    "deep": "deep-delegation",
    "deep-delegate": "deep-delegation",
    "deep-delegation": "deep-delegation",
    "aggressive": "deep-delegation",
    "aggressive-delegation": "deep-delegation",
    "quality": "orchestrator",
    "conservative": "orchestrator",
    "codex-control": "orchestrator",
    "orchestrator": "orchestrator",
    "manual": "manual",
    "opt-in": "manual",
    "provider-only": "manual",
}


def template_text(relative_path: str) -> str:
    return files("subagent_router.templates").joinpath(relative_path).read_text(
        encoding="utf-8"
    )


def normalize_profile(raw: str | None) -> str:
    if raw is None or str(raw).strip() == "":
        return DEFAULT_PROFILE
    key = str(raw).strip().lower()
    try:
        return PROFILE_ALIASES[key]
    except KeyError as exc:
        accepted = ", ".join(CANONICAL_PROFILES)
        aliases = ", ".join(sorted(PROFILE_ALIASES))
        raise ValueError(
            f"invalid delegation profile {raw!r}; expected one of: {accepted}; aliases: {aliases}"
        ) from exc


def subagent_router_instructions_for_profile(profile: str | None) -> str:
    canonical = normalize_profile(profile)
    if canonical == "manual":
        raise ValueError("manual profile does not install global instructions")
    return template_text(f"profiles/{canonical}/SUBAGENT_ROUTER_INSTRUCTIONS.md")


SUBAGENT_ROUTER_SYSTEM_INSTRUCTIONS = subagent_router_instructions_for_profile(DEFAULT_PROFILE)
DEEPSEEK_SKILL = template_text("skills/deepseek/SKILL.md")
DEEPSEEK_SLASH_COMMAND = template_text("slash_commands/deepseek.md")
EXPLORER_AGENT = template_text("agents/subagent-router-explorer.toml")
WORKER_AGENT = template_text("agents/subagent-router-worker.toml")
REVIEWER_AGENT = template_text("agents/subagent-router-reviewer.toml")
