from __future__ import annotations

from importlib.resources import files


def template_text(relative_path: str) -> str:
    return files("subagent_router.templates").joinpath(relative_path).read_text(
        encoding="utf-8"
    )


SUBAGENT_ROUTER_SYSTEM_INSTRUCTIONS = template_text("SUBAGENT_ROUTER_INSTRUCTIONS.md")
DEEPSEEK_SKILL = template_text("skills/deepseek/SKILL.md")
DEEPSEEK_SLASH_COMMAND = template_text("slash_commands/deepseek.md")
WORKER_AGENT = template_text("agents/subagent-router-worker.toml")
REVIEWER_AGENT = template_text("agents/subagent-router-reviewer.toml")
