from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def _default_state_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return $XDG_STATE_HOME/subagent-router or ~/.local/state/subagent-router."""
    source = os.environ if env is None else env
    xdg = source.get("XDG_STATE_HOME")
    if xdg:
        return (Path(xdg).expanduser() / "subagent-router").resolve()
    home = Path(source["HOME"]).expanduser() if source.get("HOME") else Path.home()
    return (home / ".local" / "state" / "subagent-router").resolve()


def _resolve_under_state_dir(raw: str, state_dir: Path) -> Path:
    """Expand ~ and absolutize; relative paths resolve under state_dir."""
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p
    return (state_dir / p).resolve()


def _env_value(source: Mapping[str, str], key: str, legacy_key: str | None = None, default: str | None = None) -> str | None:
    if key in source:
        return source.get(key)
    if legacy_key and legacy_key in source:
        return source.get(legacy_key)
    return default


@dataclass
class Settings:
    """Centralized, env-backed configuration for the Subagent Router.

    All paths are resolved once at startup.  Relative values for log, activity,
    session mirror, and provider-error paths are resolved under ``state_dir``.
    User-provided paths are expanded (``~``) and absolutized.
    """

    # API configuration (provider-specific; DeepSeek env vars kept as-is).
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str | None = None
    send_parallel_tool_calls: bool = False
    mock_deepseek: bool = False
    allow_apply_patch: bool = False
    trace_enabled: bool = False

    # Server configuration.
    host: str = "127.0.0.1"
    port: int = 8787

    # Resolved paths.
    state_dir: Path = Path()
    log_dir: Path = Path()
    activity_file: Path = Path()
    session_mirror_file: Path = Path()
    provider_error_log_dir: Path = Path()

    @staticmethod
    def from_env(env: Mapping[str, str] | None = None) -> Settings:
        """Build a Settings from environment variables, applying defaults."""
        source = os.environ if env is None else env
        raw_state = _env_value(source, "SUBAGENT_ROUTER_STATE_DIR", "CODEX_PROXY_STATE_DIR")
        if raw_state:
            state_dir = Path(raw_state).expanduser().resolve()
        else:
            state_dir = _default_state_dir(source)

        return Settings(
            state_dir=state_dir,
            deepseek_api_key=source.get("DEEPSEEK_API_KEY"),
            deepseek_base_url=source.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            deepseek_model=source.get("DEEPSEEK_MODEL"),
            send_parallel_tool_calls=source.get("DEEPSEEK_SEND_PARALLEL_TOOL_CALLS") == "1",
            mock_deepseek=source.get("DEEPSEEK_PROXY_MOCK") == "1",
            allow_apply_patch=source.get("DEEPSEEK_ALLOW_APPLY_PATCH") == "1",
            trace_enabled=_env_value(source, "SUBAGENT_ROUTER_TRACE", "CODEX_PROXY_TRACE") == "1",
            host=_env_value(source, "SUBAGENT_ROUTER_HOST", "CODEX_PROXY_HOST", "127.0.0.1") or "127.0.0.1",
            port=int(_env_value(source, "SUBAGENT_ROUTER_PORT", "CODEX_PROXY_PORT", "8787") or "8787"),
            log_dir=_resolve_under_state_dir(
                _env_value(source, "SUBAGENT_ROUTER_LOG_DIR", "CODEX_PROXY_LOG_DIR", "logs/client_payloads") or "logs/client_payloads",
                state_dir,
            ),
            activity_file=_resolve_under_state_dir(
                _env_value(source, "SUBAGENT_ROUTER_ACTIVITY_FILE", "CODEX_PROXY_ACTIVITY_FILE", "logs/activity.json") or "logs/activity.json",
                state_dir,
            ),
            session_mirror_file=_resolve_under_state_dir(
                _env_value(source, "SUBAGENT_ROUTER_SESSION_MIRROR_FILE", "CODEX_PROXY_SESSION_MIRROR_FILE", "logs/session_mirror.json") or "logs/session_mirror.json",
                state_dir,
            ),
            provider_error_log_dir=_resolve_under_state_dir(
                _env_value(source, "SUBAGENT_ROUTER_PROVIDER_ERROR_LOG_DIR", "CODEX_PROXY_PROVIDER_ERROR_LOG_DIR", "logs/provider_errors") or "logs/provider_errors",
                state_dir,
            ),
        )

    def as_env(self, include_secrets: bool = False) -> dict[str, str]:
        """Return environment variables that reproduce these resolved settings.

        By default, secrets (e.g. DEEPSEEK_API_KEY) are excluded.
        Pass include_secrets=True to include them, for example when
        building the proxy-server subprocess environment.
        """
        env = {
            "DEEPSEEK_BASE_URL": self.deepseek_base_url,
            "DEEPSEEK_SEND_PARALLEL_TOOL_CALLS": "1" if self.send_parallel_tool_calls else "0",
            "DEEPSEEK_PROXY_MOCK": "1" if self.mock_deepseek else "0",
            "DEEPSEEK_ALLOW_APPLY_PATCH": "1" if self.allow_apply_patch else "0",
            "SUBAGENT_ROUTER_TRACE": "1" if self.trace_enabled else "0",
            "SUBAGENT_ROUTER_HOST": self.host,
            "SUBAGENT_ROUTER_PORT": str(self.port),
            "SUBAGENT_ROUTER_STATE_DIR": str(self.state_dir),
            "SUBAGENT_ROUTER_LOG_DIR": str(self.log_dir),
            "SUBAGENT_ROUTER_ACTIVITY_FILE": str(self.activity_file),
            "SUBAGENT_ROUTER_SESSION_MIRROR_FILE": str(self.session_mirror_file),
            "SUBAGENT_ROUTER_PROVIDER_ERROR_LOG_DIR": str(self.provider_error_log_dir),
        }
        if include_secrets and self.deepseek_api_key:
            env["DEEPSEEK_API_KEY"] = self.deepseek_api_key
        if self.deepseek_model:
            env["DEEPSEEK_MODEL"] = self.deepseek_model
        return env

    def sanitized_paths(self) -> dict[str, str]:
        """Return sanitized string representations of all resolved paths."""
        return {
            "state_dir": str(self.state_dir),
            "log_dir": str(self.log_dir),
            "activity_file": str(self.activity_file),
            "session_mirror_file": str(self.session_mirror_file),
            "provider_error_log_dir": str(self.provider_error_log_dir),
        }
