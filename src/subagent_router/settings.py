from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping
import tomllib

from .providers import ProviderCapabilities, ProviderConfig


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


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _config_paths(env: Mapping[str, str]) -> list[Path]:
    explicit = env.get("SUBAGENT_ROUTER_CONFIG")
    if explicit:
        return [Path(explicit).expanduser().resolve()]
    home = Path(env["HOME"]).expanduser() if env.get("HOME") else Path.home()
    return [home / ".config" / "subagent-router" / "config.toml"]


def _load_file_config(env: Mapping[str, str]) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for path in _config_paths(env):
        data = _read_toml(path)
        if data:
            config = _deep_merge(config, data)
    return config


def config_permission_warnings(env: Mapping[str, str] | None = None) -> list[str]:
    """Return warnings for config files readable/writable by group or others."""
    source = os.environ if env is None else env
    warnings: list[str] = []
    for path in _config_paths(source):
        try:
            mode = path.stat().st_mode
        except OSError:
            continue
        if mode & 0o077:
            warnings.append(f"{path}: permissions are too broad; use chmod 600")
    return warnings


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _bool_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on"}


def _int_value(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_value(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _list_value(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _float_map(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, float] = {}
    for key, raw in value.items():
        parsed = _float_value(raw)
        if parsed is not None:
            result[str(key)] = parsed
    return result


def _int_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for key, raw in value.items():
        parsed = _int_value(raw)
        if parsed is not None:
            result[str(key)] = parsed
    return result


def _config_value(config: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = config
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _capabilities(raw: Mapping[str, Any] | None, *, kind: str, provider_type: str) -> ProviderCapabilities:
    data = dict(raw or {})
    return ProviderCapabilities(
        context_window=_int_value(data.get("context_window")),
        supports_tools=_bool_value(data.get("tool_support"), True),
        supports_parallel_tool_calls=_bool_value(data.get("parallel_tool_calls_support"), provider_type != "ollama"),
        supports_streaming=_bool_value(data.get("streaming_support"), provider_type != "ollama"),
        cost_hint=str(data.get("cost_hint") or ("zero-api-cost" if kind == "local" else "unknown")),
        input_cost_per_million=_float_value(data.get("input_cost_per_million")),
        output_cost_per_million=_float_value(data.get("output_cost_per_million")),
        cached_input_cost_per_million=_float_value(data.get("cached_input_cost_per_million")),
        supports_reasoning=_bool_value(data.get("reasoning_support"), False),
        supports_token_accounting=_bool_value(data.get("token_accounting_support"), True),
        supports_usage_reporting=_bool_value(data.get("usage_reporting_support"), True),
        supports_cache_tokens=_bool_value(data.get("cache_token_reporting_support"), False),
        supports_reasoning_tokens=_bool_value(data.get("reasoning_token_reporting_support"), False),
        timeout_seconds=_float_value(data.get("timeout_seconds")),
        retry_count=_int_value(data.get("retry_count"), 0) or 0,
    )


def _provider_configs(source: Mapping[str, str], file_config: dict[str, Any]) -> dict[str, ProviderConfig]:
    provider_blocks = _config_value(file_config, "providers", {})
    providers: dict[str, ProviderConfig] = {}
    if isinstance(provider_blocks, dict):
        for name, raw in provider_blocks.items():
            if not isinstance(raw, dict):
                continue
            provider_type = str(raw.get("type") or raw.get("provider_type") or "openai-compatible")
            kind = str(raw.get("kind") or ("local" if provider_type == "ollama" else "cloud"))
            capabilities = _capabilities(raw.get("capabilities") if isinstance(raw.get("capabilities"), dict) else raw, kind=kind, provider_type=provider_type)
            providers[str(name)] = ProviderConfig(
                name=str(name),
                kind=kind,
                provider_type=provider_type,
                base_url=str(raw.get("base_url") or ""),
                api_key=str(raw["api_key"]) if raw.get("api_key") else None,
                model=str(raw["model"]) if raw.get("model") else None,
                capabilities=capabilities,
                enabled=_bool_value(raw.get("enabled"), True),
                timeout_seconds=_float_value(raw.get("timeout_seconds"), capabilities.timeout_seconds),
                send_parallel_tool_calls=_bool_value(raw.get("send_parallel_tool_calls"), False),
                explorer_model=str(raw["explorer_model"]) if raw.get("explorer_model") else None,
                worker_model=str(raw["worker_model"]) if raw.get("worker_model") else None,
                reviewer_model=str(raw["reviewer_model"]) if raw.get("reviewer_model") else None,
            )

    deepseek_block = provider_blocks.get("deepseek", {}) if isinstance(provider_blocks, dict) else {}
    if not isinstance(deepseek_block, dict):
        deepseek_block = {}
    deepseek_base_url = (
        source.get("DEEPSEEK_BASE_URL")
        or deepseek_block.get("base_url")
        or _config_value(file_config, "deepseek.base_url", "https://api.deepseek.com/v1")
    )
    deepseek_model = source.get("DEEPSEEK_MODEL") or deepseek_block.get("model") or _config_value(file_config, "deepseek.model")
    deepseek_capabilities = _capabilities(
        deepseek_block.get("capabilities") if isinstance(deepseek_block.get("capabilities"), dict) else deepseek_block,
        kind="cloud",
        provider_type="deepseek",
    )
    providers["deepseek"] = ProviderConfig(
        name="deepseek",
        kind="cloud",
        provider_type="deepseek",
        base_url=str(deepseek_base_url),
        api_key=source.get("DEEPSEEK_API_KEY") or deepseek_block.get("api_key") or _config_value(file_config, "deepseek.api_key"),
        model=str(deepseek_model) if deepseek_model else None,
        capabilities=ProviderCapabilities(
            context_window=deepseek_capabilities.context_window or 128000,
            supports_tools=deepseek_capabilities.supports_tools,
            supports_streaming=deepseek_capabilities.supports_streaming,
            supports_parallel_tool_calls=deepseek_capabilities.supports_parallel_tool_calls,
            cost_hint=deepseek_capabilities.cost_hint if deepseek_capabilities.cost_hint != "unknown" else "paid-api",
            input_cost_per_million=deepseek_capabilities.input_cost_per_million,
            output_cost_per_million=deepseek_capabilities.output_cost_per_million,
            cached_input_cost_per_million=deepseek_capabilities.cached_input_cost_per_million,
            supports_reasoning=True,
            supports_token_accounting=deepseek_capabilities.supports_token_accounting,
            supports_usage_reporting=deepseek_capabilities.supports_usage_reporting,
            supports_cache_tokens=deepseek_capabilities.supports_cache_tokens,
            supports_reasoning_tokens=True,
        ),
        enabled=_bool_value(deepseek_block.get("enabled"), True),
        timeout_seconds=_float_value(deepseek_block.get("timeout_seconds") or _config_value(file_config, "deepseek.timeout_seconds")),
        send_parallel_tool_calls=source.get("DEEPSEEK_SEND_PARALLEL_TOOL_CALLS") == "1" or _bool_value(deepseek_block.get("send_parallel_tool_calls"), False),
        explorer_model=str(deepseek_block["explorer_model"]) if deepseek_block.get("explorer_model") else None,
        worker_model=str(deepseek_block["worker_model"]) if deepseek_block.get("worker_model") else None,
        reviewer_model=str(deepseek_block["reviewer_model"]) if deepseek_block.get("reviewer_model") else None,
    )

    if "openai-compatible" not in providers:
        providers["openai-compatible"] = ProviderConfig(
            name="openai-compatible",
            kind="cloud",
            provider_type="openai-compatible",
            base_url=str(source.get("OPENAI_COMPAT_BASE_URL") or _config_value(file_config, "openai_compatible.base_url", "")),
            api_key=source.get("OPENAI_COMPAT_API_KEY") or _config_value(file_config, "openai_compatible.api_key"),
            model=source.get("OPENAI_COMPAT_MODEL") or _config_value(file_config, "openai_compatible.model"),
            capabilities=ProviderCapabilities(supports_tools=True, supports_streaming=False, cost_hint="configured-api"),
            enabled=bool(source.get("OPENAI_COMPAT_BASE_URL") or _config_value(file_config, "openai_compatible.base_url")),
        )
    if "groq" not in providers:
        providers["groq"] = ProviderConfig(
            name="groq",
            kind="cloud",
            provider_type="openai-compatible",
            base_url=str(source.get("GROQ_BASE_URL") or _config_value(file_config, "groq.base_url", "https://api.groq.com/openai/v1")),
            api_key=source.get("GROQ_API_KEY") or _config_value(file_config, "groq.api_key"),
            model=source.get("GROQ_MODEL") or _config_value(file_config, "groq.model", "llama-3.3-70b-versatile"),
            capabilities=ProviderCapabilities(supports_tools=True, supports_streaming=False, cost_hint="paid-api"),
            enabled=bool(source.get("GROQ_API_KEY") or _config_value(file_config, "groq.api_key")),
        )

    if "ollama" not in providers:
        providers["ollama"] = ProviderConfig(
            name="ollama",
            kind="local",
            provider_type="ollama",
            base_url=str(source.get("OLLAMA_BASE_URL") or _config_value(file_config, "ollama.base_url", "http://127.0.0.1:11434")),
            model=source.get("OLLAMA_MODEL") or _config_value(file_config, "ollama.model", "llama3.1"),
            capabilities=ProviderCapabilities(
                supports_tools=True,
                supports_streaming=False,
                cost_hint="zero-api-cost",
                supports_usage_reporting=False,
            ),
            enabled=_bool_value(source.get("OLLAMA_ENABLED") or _config_value(file_config, "ollama.enabled"), False),
        )

    return providers


@dataclass(frozen=True)
class ProviderPrediction:
    """Structured prediction inputs for advanced routing policy."""
    reliability_score: float | None = None  # 0.0-1.0 expected success rate
    latency_p50_ms: float | None = None     # expected median latency in ms
    latency_p99_ms: float | None = None     # expected p99 latency in ms
    capability_score: float | None = None   # 0.0-1.0 capability metric (tool support, accuracy, etc.)
    budget_prediction_usd: float | None = None  # predicted cost per request
    budget_prediction_tokens: int | None = None # predicted token consumption per request


@dataclass(frozen=True)
class ProviderHealthMetadata:
    """Runtime health snapshot for a provider."""
    last_error_at: float | None = None
    last_success_at: float | None = None
    consecutive_errors: int = 0
    total_requests: int = 0
    total_errors: int = 0
    error_rate: float = 0.0  # 0.0-1.0
    average_latency_ms: float = 0.0
    uptime_fraction: float = 1.0  # 0.0-1.0 over observed window




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
    provider: str = "deepseek"
    fallback_providers: list[str] = field(default_factory=list)
    routing_policies: dict[str, dict[str, Any]] = field(default_factory=dict)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    max_cost_per_task: float | None = None
    max_tokens_per_task: int | None = None
    max_cost_per_provider: dict[str, float] = field(default_factory=dict)
    max_tokens_per_provider: dict[str, int] = field(default_factory=dict)
    max_cost_per_model: dict[str, float] = field(default_factory=dict)
    max_tokens_per_model: dict[str, int] = field(default_factory=dict)
    max_cost_per_day: float | None = None
    max_tokens_per_day: int | None = None
    max_cost_per_session: float | None = None
    max_tokens_per_session: int | None = None
    budget_mode: str = "warn"
    allowed_providers: list[str] = field(default_factory=list)
    denied_providers: list[str] = field(default_factory=list)
    dry_run: bool = False
    config_warnings: list[str] = field(default_factory=list)
    debug_mode: bool = False
    provider_predictions: dict[str, ProviderPrediction] = field(default_factory=dict)
    provider_health: dict[str, ProviderHealthMetadata] = field(default_factory=dict)
    max_spend_per_task: float | None = None
    max_spend_per_provider: dict[str, float] = field(default_factory=dict)
    max_spend_per_model: dict[str, float] = field(default_factory=dict)
    max_spend_per_day: float | None = None
    max_spend_per_session: float | None = None

    # Server configuration.
    host: str = "127.0.0.1"
    port: int = 8787
    codex_home: Path | None = None

    # Resolved paths.
    state_dir: Path = Path()
    log_dir: Path = Path()
    activity_file: Path = Path()
    session_mirror_file: Path = Path()
    provider_error_log_dir: Path = Path()
    audit_log_file: Path = Path()
    usage_file: Path = Path()
    usage_jsonl_file: Path = Path()

    @staticmethod
    def from_env(env: Mapping[str, str] | None = None) -> Settings:
        """Build a Settings from environment variables, applying defaults."""
        source = os.environ if env is None else env
        file_config = _load_file_config(source)
        raw_state = _env_value(source, "SUBAGENT_ROUTER_STATE_DIR", "CODEX_PROXY_STATE_DIR")
        if raw_state is None:
            raw_state = _config_value(file_config, "state_dir")
        if raw_state:
            state_dir = Path(raw_state).expanduser().resolve()
        else:
            state_dir = _default_state_dir(source)
        providers = _provider_configs(source, file_config)
        provider = source.get("SUBAGENT_ROUTER_PROVIDER") or _config_value(file_config, "defaults.provider", "deepseek")
        fallback_raw = source.get("SUBAGENT_ROUTER_FALLBACK_PROVIDERS") or _config_value(file_config, "defaults.fallback_providers", [])
        if isinstance(fallback_raw, str):
            fallback_providers = [item.strip() for item in fallback_raw.split(",") if item.strip()]
        elif isinstance(fallback_raw, list):
            fallback_providers = [str(item) for item in fallback_raw]
        else:
            fallback_providers = []
        route_config = _config_value(file_config, "routes", {})
        routing_policies = route_config if isinstance(route_config, dict) else {}
        allow_raw = source.get("SUBAGENT_ROUTER_PROVIDER_ALLOWLIST") or _config_value(file_config, "security.provider_allowlist", [])
        deny_raw = source.get("SUBAGENT_ROUTER_PROVIDER_DENYLIST") or _config_value(file_config, "security.provider_denylist", [])
        allowed_providers = _list_value(allow_raw)
        denied_providers = _list_value(deny_raw)
        provider_cost_limits = _float_map(_config_value(file_config, "budgets.provider_max_cost_per_task", {}))
        provider_token_limits = _int_map(_config_value(file_config, "budgets.provider_max_tokens_per_task", {}))
        model_cost_limits = _float_map(_config_value(file_config, "budgets.model_max_cost_per_task", {}))
        model_token_limits = _int_map(_config_value(file_config, "budgets.model_max_tokens_per_task", {}))
        max_cost_per_day = _float_value(source.get("SUBAGENT_ROUTER_MAX_COST_PER_DAY") or _config_value(file_config, "budgets.max_cost_per_day"))
        max_tokens_per_day = _int_value(source.get("SUBAGENT_ROUTER_MAX_TOKENS_PER_DAY") or _config_value(file_config, "budgets.max_tokens_per_day"))
        max_cost_per_session = _float_value(source.get("SUBAGENT_ROUTER_MAX_COST_PER_SESSION") or _config_value(file_config, "budgets.max_cost_per_session"))
        max_tokens_per_session = _int_value(source.get("SUBAGENT_ROUTER_MAX_TOKENS_PER_SESSION") or _config_value(file_config, "budgets.max_tokens_per_session"))
        max_spend_per_task = _float_value(source.get("SUBAGENT_ROUTER_MAX_SPEND_PER_TASK") or _config_value(file_config, "budgets.max_spend_per_task"))
        max_spend_per_day = _float_value(source.get("SUBAGENT_ROUTER_MAX_SPEND_PER_DAY") or _config_value(file_config, "budgets.max_spend_per_day"))
        max_spend_per_session = _float_value(source.get("SUBAGENT_ROUTER_MAX_SPEND_PER_SESSION") or _config_value(file_config, "budgets.max_spend_per_session"))
        provider_spend_limits = _float_map(_config_value(file_config, "budgets.provider_max_spend_per_task", {}))
        model_spend_limits = _float_map(_config_value(file_config, "budgets.model_max_spend_per_task", {}))
        debug_mode = _bool_value(source.get("SUBAGENT_ROUTER_DEBUG") or _config_value(file_config, "debug"), False)
        config_warnings = config_permission_warnings(source)
        if debug_mode:
            config_warnings.append("debug mode is enabled; avoid using it with sensitive prompts or secrets")
        raw_predictions = _config_value(file_config, "predictions", {})
        provider_predictions = {}
        if isinstance(raw_predictions, dict):
            for pname, pdata in raw_predictions.items():
                if isinstance(pdata, dict):
                    provider_predictions[str(pname)] = ProviderPrediction(
                        reliability_score=_float_value(pdata.get("reliability_score")),
                        latency_p50_ms=_float_value(pdata.get("latency_p50_ms")),
                        latency_p99_ms=_float_value(pdata.get("latency_p99_ms")),
                        capability_score=_float_value(pdata.get("capability_score")),
                        budget_prediction_usd=_float_value(pdata.get("budget_prediction_usd")),
                        budget_prediction_tokens=_int_value(pdata.get("budget_prediction_tokens")),
                    )

        # Ensure that explicitly selected default or fallback providers are enabled
        # in the configuration map, even if their OLLAMA_ENABLED-style vars are missing.
        active_names = {str(provider)} | set(fallback_providers)
        for name in active_names:
            if name in providers and not providers[name].enabled:
                providers[name] = replace(providers[name], enabled=True)

        settings = Settings(
            state_dir=state_dir,
            deepseek_api_key=providers["deepseek"].api_key,
            deepseek_base_url=providers["deepseek"].base_url,
            deepseek_model=providers["deepseek"].model,
            send_parallel_tool_calls=source.get("DEEPSEEK_SEND_PARALLEL_TOOL_CALLS") == "1",
            mock_deepseek=source.get("DEEPSEEK_PROXY_MOCK") == "1",
            allow_apply_patch=source.get("DEEPSEEK_ALLOW_APPLY_PATCH") == "1",
            trace_enabled=_env_value(source, "SUBAGENT_ROUTER_TRACE", "CODEX_PROXY_TRACE") == "1",
            host=_env_value(source, "SUBAGENT_ROUTER_HOST", "CODEX_PROXY_HOST", "127.0.0.1") or "127.0.0.1",
            port=int(_env_value(source, "SUBAGENT_ROUTER_PORT", "CODEX_PROXY_PORT", "8787") or "8787"),
            codex_home=Path(source["CODEX_HOME"]).expanduser().resolve() if source.get("CODEX_HOME") else None,
            provider=str(provider),
            fallback_providers=fallback_providers,
            routing_policies=routing_policies,
            providers=providers,
            max_cost_per_task=_float_value(source.get("SUBAGENT_ROUTER_MAX_COST_PER_TASK") or _config_value(file_config, "budgets.max_cost_per_task")),
            max_tokens_per_task=_int_value(source.get("SUBAGENT_ROUTER_MAX_TOKENS_PER_TASK") or _config_value(file_config, "budgets.max_tokens_per_task")),
            max_cost_per_provider=provider_cost_limits,
            max_tokens_per_provider=provider_token_limits,
            max_cost_per_model=model_cost_limits,
            max_tokens_per_model=model_token_limits,
            max_cost_per_day=max_cost_per_day,
            max_tokens_per_day=max_tokens_per_day,
            max_cost_per_session=max_cost_per_session,
            max_tokens_per_session=max_tokens_per_session,
            budget_mode=str(source.get("SUBAGENT_ROUTER_BUDGET_MODE") or _config_value(file_config, "budgets.mode", "warn")),
            allowed_providers=allowed_providers,
            denied_providers=denied_providers,
            dry_run=_bool_value(source.get("SUBAGENT_ROUTER_DRY_RUN") or _config_value(file_config, "dry_run"), False),
            config_warnings=config_warnings,
            debug_mode=debug_mode,
            provider_predictions=provider_predictions,
            max_spend_per_task=max_spend_per_task,
            max_spend_per_provider=provider_spend_limits,
            max_spend_per_model=model_spend_limits,
            max_spend_per_day=max_spend_per_day,
            max_spend_per_session=max_spend_per_session,
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
            audit_log_file=_resolve_under_state_dir(
                source.get("SUBAGENT_ROUTER_AUDIT_LOG_FILE") or _config_value(file_config, "logging.audit_log_file", "logs/audit.jsonl"),
                state_dir,
            ),
            usage_file=_resolve_under_state_dir(
                source.get("SUBAGENT_ROUTER_USAGE_FILE") or _config_value(file_config, "logging.usage_file", "logs/usage.json"),
                state_dir,
            ),
            usage_jsonl_file=_resolve_under_state_dir(
                source.get("SUBAGENT_ROUTER_USAGE_JSONL_FILE") or _config_value(file_config, "logging.usage_jsonl_file", "logs/usage.jsonl"),
                state_dir,
            ),
        )

        # Apply runtime overrides if any
        runtime_path = state_dir / "runtime_config.json"
        if runtime_path.exists():
            try:
                import json
                overrides = json.loads(runtime_path.read_text(encoding="utf-8"))
                updates = {}
                for key in ["provider", "fallback_providers", "budget_mode", "max_cost_per_day", "max_cost_per_session", "max_cost_per_task", "routing_policies"]:
                    if key in overrides:
                        updates[key] = overrides[key]
                if "providers" in overrides:
                    new_providers = dict(settings.providers)
                    for pname, pdata in overrides["providers"].items():
                        if pname in new_providers:
                            cap = new_providers[pname].capabilities
                            if "pricing" in pdata:
                                pricing = pdata["pricing"]
                                cap = replace(
                                    cap,
                                    input_cost_per_million=pricing.get("in", cap.input_cost_per_million),
                                    output_cost_per_million=pricing.get("out", cap.output_cost_per_million),
                                    cached_input_cost_per_million=pricing.get("cached", cap.cached_input_cost_per_million),
                                    cost_hint="configured-api" if any(k in pricing for k in ("in", "out", "cached")) else cap.cost_hint
                                )
                            
                            mp_kwargs = {}
                            if "model_pricing" in pdata and isinstance(pdata["model_pricing"], dict):
                                mp_model_pricing = {}
                                for mname, mrates in pdata["model_pricing"].items():
                                    if isinstance(mrates, dict):
                                        mp_model_pricing[mname] = {
                                            "input_cost_per_million": mrates.get("in"),
                                            "output_cost_per_million": mrates.get("out"),
                                            "cached_input_cost_per_million": mrates.get("cached"),
                                        }
                                mp_kwargs["model_pricing"] = mp_model_pricing
                            if "model" in pdata:
                                new_providers[pname] = replace(new_providers[pname], model=pdata["model"], capabilities=cap, **mp_kwargs)
                            else:
                                new_providers[pname] = replace(new_providers[pname], capabilities=cap, **mp_kwargs)

                            if "explorer_model" in pdata:
                                new_providers[pname] = replace(new_providers[pname], explorer_model=pdata["explorer_model"])
                            if "worker_model" in pdata:
                                new_providers[pname] = replace(new_providers[pname], worker_model=pdata["worker_model"])
                            if "reviewer_model" in pdata:
                                new_providers[pname] = replace(new_providers[pname], reviewer_model=pdata["reviewer_model"])

                            if "enabled" in pdata:
                                new_providers[pname] = replace(new_providers[pname], enabled=bool(pdata["enabled"]))
                    updates["providers"] = new_providers
                if updates:
                    settings = replace(settings, **updates)
            except Exception:
                pass

        return settings.ensure_active_providers_enabled()

    def ensure_active_providers_enabled(self) -> Settings:
        active_names = {self.provider} | set(self.fallback_providers)
        # Also include any providers mentioned in routing policies
        for policy in self.routing_policies.values():
            if isinstance(policy, dict) and "provider" in policy:
                active_names.add(str(policy["provider"]))

        new_providers = dict(self.providers)
        changed = False
        for name in active_names:
            if name in new_providers and not new_providers[name].enabled:
                new_providers[name] = replace(new_providers[name], enabled=True)
                changed = True

        if not changed:
            return self
        return replace(self, providers=new_providers)

    @staticmethod
    def _provider_entry(p: ProviderConfig) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "model": p.model,
            "enabled": p.enabled,
            "pricing": {
                k: v for k, v in {
                    "in": p.capabilities.input_cost_per_million,
                    "out": p.capabilities.output_cost_per_million,
                    "cached": p.capabilities.cached_input_cost_per_million,
                }.items() if v is not None
            },
            "model_pricing": {
                mname: {
                    mk: mv for mk, mv in {
                        "in": rates.get("input_cost_per_million"),
                        "out": rates.get("output_cost_per_million"),
                        "cached": rates.get("cached_input_cost_per_million"),
                    }.items() if mv is not None
                } for mname, rates in p.model_pricing.items()
            },
        }
        if p.explorer_model is not None:
            entry["explorer_model"] = p.explorer_model
        if p.worker_model is not None:
            entry["worker_model"] = p.worker_model
        if p.reviewer_model is not None:
            entry["reviewer_model"] = p.reviewer_model
        return entry

    def save_runtime_config(self) -> None:
        import json
        path = self.state_dir / "runtime_config.json"
        data = {
            "provider": self.provider,
            "fallback_providers": self.fallback_providers,
            "budget_mode": self.budget_mode,
            "max_cost_per_day": self.max_cost_per_day,
            "max_cost_per_session": self.max_cost_per_session,
            "max_cost_per_task": self.max_cost_per_task,
            "routing_policies": self.routing_policies,
            "providers": {
                name: self._provider_entry(p) for name, p in self.providers.items()
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

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
            "SUBAGENT_ROUTER_AUDIT_LOG_FILE": str(self.audit_log_file),
            "SUBAGENT_ROUTER_USAGE_FILE": str(self.usage_file),
            "SUBAGENT_ROUTER_USAGE_JSONL_FILE": str(self.usage_jsonl_file),
            "SUBAGENT_ROUTER_PROVIDER": self.provider,
            "SUBAGENT_ROUTER_FALLBACK_PROVIDERS": ",".join(self.fallback_providers),
            "SUBAGENT_ROUTER_BUDGET_MODE": self.budget_mode,
            "SUBAGENT_ROUTER_PROVIDER_ALLOWLIST": ",".join(self.allowed_providers),
            "SUBAGENT_ROUTER_PROVIDER_DENYLIST": ",".join(self.denied_providers),
            "SUBAGENT_ROUTER_DRY_RUN": "1" if self.dry_run else "0",
        }
        if self.codex_home is not None:
            env["CODEX_HOME"] = str(self.codex_home)
        if self.max_cost_per_task is not None:
            env["SUBAGENT_ROUTER_MAX_COST_PER_TASK"] = str(self.max_cost_per_task)
        if self.max_tokens_per_task is not None:
            env["SUBAGENT_ROUTER_MAX_TOKENS_PER_TASK"] = str(self.max_tokens_per_task)
        if self.max_cost_per_day is not None:
            env["SUBAGENT_ROUTER_MAX_COST_PER_DAY"] = str(self.max_cost_per_day)
        if self.max_tokens_per_day is not None:
            env["SUBAGENT_ROUTER_MAX_TOKENS_PER_DAY"] = str(self.max_tokens_per_day)
        if self.max_cost_per_session is not None:
            env["SUBAGENT_ROUTER_MAX_COST_PER_SESSION"] = str(self.max_cost_per_session)
        if self.max_tokens_per_session is not None:
            env["SUBAGENT_ROUTER_MAX_TOKENS_PER_SESSION"] = str(self.max_tokens_per_session)
        if self.max_spend_per_task is not None:
            env["SUBAGENT_ROUTER_MAX_SPEND_PER_TASK"] = str(self.max_spend_per_task)
        if self.max_spend_per_day is not None:
            env["SUBAGENT_ROUTER_MAX_SPEND_PER_DAY"] = str(self.max_spend_per_day)
        if self.max_spend_per_session is not None:
            env["SUBAGENT_ROUTER_MAX_SPEND_PER_SESSION"] = str(self.max_spend_per_session)
        if self.debug_mode:
            env["SUBAGENT_ROUTER_DEBUG"] = "1"
        if include_secrets and self.deepseek_api_key:
            env["DEEPSEEK_API_KEY"] = self.deepseek_api_key
        if self.deepseek_model:
            env["DEEPSEEK_MODEL"] = self.deepseek_model
        openai = self.providers.get("openai-compatible")
        if openai:
            if openai.base_url:
                env["OPENAI_COMPAT_BASE_URL"] = openai.base_url
            if openai.model:
                env["OPENAI_COMPAT_MODEL"] = openai.model
            if include_secrets and openai.api_key:
                env["OPENAI_COMPAT_API_KEY"] = openai.api_key
        ollama = self.providers.get("ollama")
        if ollama:
            env["OLLAMA_BASE_URL"] = ollama.base_url
            if ollama.model:
                env["OLLAMA_MODEL"] = ollama.model
            env["OLLAMA_ENABLED"] = "1" if ollama.enabled else "0"
        return env

    def apply_patch_override(self) -> bool | None:
        """Return the explicit apply_patch override for request normalization."""
        return True if self.allow_apply_patch else None

    def sanitized_paths(self) -> dict[str, str]:
        """Return sanitized string representations of all resolved paths."""
        return {
            "state_dir": str(self.state_dir),
            "log_dir": str(self.log_dir),
            "activity_file": str(self.activity_file),
            "session_mirror_file": str(self.session_mirror_file),
            "provider_error_log_dir": str(self.provider_error_log_dir),
            "audit_log_file": str(self.audit_log_file),
            "usage_file": str(self.usage_file),
            "usage_jsonl_file": str(self.usage_jsonl_file),
        }
