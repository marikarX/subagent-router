from __future__ import annotations

import argparse
import ast
import asyncio
from collections import deque
import hashlib
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import select
import tarfile
import time
import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Sequence
from urllib.request import urlopen

from .activation import (
    CANONICAL_PROFILES,
    DEFAULT_PROFILE,
    DEEPSEEK_SKILL,
    DEEPSEEK_SLASH_COMMAND,
    EXPLORER_AGENT,
    REVIEWER_AGENT,
    WORKER_AGENT,
    normalize_profile,
    subagent_router_instructions_for_profile,
)
from .settings import Settings


PROVIDER_ID = "subagent_router"
PROVIDER_NAME = "Subagent Router"
CONFIG_MARKER_BEGIN = "# >>> subagent-router >>>"
CONFIG_MARKER_END = "# <<< subagent-router <<<"
MANIFEST_FILENAME = ".subagent-router-manifest.json"
LEGACY_MANAGED_HASHES: dict[str, tuple[str, ...]] = {
    "SUBAGENT_ROUTER_INSTRUCTIONS.md": ("3336ea091517b6362249cb926c7f49d76d15d47731d6985ca6ae236df511acd4",),
    "agents/subagent-router-worker.toml": ("2e99a23c2058cd8791715066385baba61113d39a5cb53abbc792a8cb92d937ac",),
    "agents/subagent-router-reviewer.toml": ("15ab6ae27eb9944f32afd9b50be70746a4c6838dac65c49942ee6d2d2183f7ba",),
}
_CHILD_SECRET_KEYS: frozenset[str] = frozenset({
    "DEEPSEEK_API_KEY",
    "OPENAI_COMPAT_API_KEY",
})


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subagent-router",
        description="Manage the local Subagent Router.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""common workflows:
  subagent-router start --background
  subagent-router start --background --attach-logs
  subagent-router logs --follow
  subagent-router restart
  subagent-router stop
  subagent-router version
  subagent-router run -- codex ...
""",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    start = subcommands.add_parser("start", help="start the local HTTP proxy")
    add_common_settings_args(start)
    start.add_argument("--background", action="store_true", help="start in the background")
    start.add_argument(
        "--attach-logs",
        action="store_true",
        help="with --background, attach to the server log after startup",
    )
    start.set_defaults(handler=cmd_start)

    stop = subcommands.add_parser("stop", help="stop a background proxy")
    add_common_settings_args(stop)
    stop.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for graceful stop")
    stop.add_argument("--force", action="store_true", help="kill the process if it does not stop")
    stop.set_defaults(handler=cmd_stop)

    restart = subcommands.add_parser("restart", help="restart a background proxy")
    add_common_settings_args(restart)
    restart.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for graceful stop")
    restart.add_argument("--force", action="store_true", help="kill the process if it does not stop")
    restart.add_argument(
        "--attach-logs",
        action="store_true",
        help="attach to the server log after restart",
    )
    restart.set_defaults(handler=cmd_restart)

    status = subcommands.add_parser("status", help="print background proxy status")
    add_common_settings_args(status)
    status.set_defaults(handler=cmd_status)

    logs = subcommands.add_parser("logs", help="print or follow the background proxy log")
    add_common_settings_args(logs)
    logs.add_argument("-f", "--follow", action="store_true", help="follow log output")
    logs.add_argument("--lines", type=int, default=80, help="number of trailing lines to print")
    logs.add_argument("--audit", action="store_true", help="print the audit JSONL log instead of server output")
    logs.set_defaults(handler=cmd_logs)

    usage = subcommands.add_parser("usage", help="print usage and cost summary")
    add_common_settings_args(usage)
    usage.add_argument("--json", action="store_true", help="print JSON")
    usage.set_defaults(handler=cmd_usage)

    debug_bundle = subcommands.add_parser("debug-bundle", help="write a redacted troubleshooting bundle")
    add_common_settings_args(debug_bundle)
    debug_bundle.add_argument("--output", default=None, help="output .tar.gz path")
    debug_bundle.set_defaults(handler=cmd_debug_bundle)

    stdio = subcommands.add_parser("stdio", help="read one Responses JSON request from stdin and write one JSON response")
    add_common_settings_args(stdio)
    stdio.set_defaults(handler=cmd_stdio)

    handoff = subcommands.add_parser("handoff", help="process JSON task files from a directory")
    add_common_settings_args(handoff)
    handoff.add_argument("--input-dir", required=True)
    handoff.add_argument("--output-dir", default=None)
    handoff.add_argument("--once", action="store_true", help="process currently available files and exit")
    handoff.add_argument("--poll-interval", type=float, default=1.0)
    handoff.set_defaults(handler=cmd_handoff)

    tui = subcommands.add_parser("tui", help="print a compact terminal status view")
    add_common_settings_args(tui)
    tui.add_argument("--codex-home", default=os.getenv("CODEX_HOME", "~/.codex"))
    tui.add_argument("--watch", action="store_true", help="continuously refresh status every 2 seconds")
    tui.set_defaults(handler=cmd_tui)

    run = subcommands.add_parser("run", help="start the proxy for one command")
    add_common_settings_args(run)
    run.add_argument("cmd", nargs=argparse.REMAINDER, help="command to run after --")
    run.set_defaults(handler=cmd_run)

    paths = subcommands.add_parser("paths", help="print resolved proxy paths")
    add_common_settings_args(paths)
    paths.add_argument("--json", action="store_true", help="print JSON")
    paths.set_defaults(handler=cmd_paths)

    version = subcommands.add_parser("version", help="print the package version")
    version.add_argument("--json", action="store_true", help="print JSON")
    version.set_defaults(handler=cmd_version)

    doctor = subcommands.add_parser("doctor", help="check local proxy configuration")
    add_common_settings_args(doctor)
    doctor.add_argument("--json", action="store_true", help="print JSON")
    doctor.set_defaults(handler=cmd_doctor)

    init = subcommands.add_parser("init", help="install Codex integration files")
    init.add_argument("--codex-home", default=os.getenv("CODEX_HOME", "~/.codex"))
    init.add_argument("--proxy-url", default="http://127.0.0.1:8787/v1")
    init.add_argument(
        "--mode",
        choices=("default", "opt-in", "provider-only"),
        default="default",
        help=(
            "default installs SUBAGENT_ROUTER_INSTRUCTIONS.md and references it from AGENTS.md; "
            "opt-in installs $deepseek and /deepseek activation only; "
            "provider-only writes provider config and agents only"
        ),
    )
    init.add_argument("--force", action="store_true", help="overwrite managed activation files")
    init.add_argument(
        "--profile",
        default=None,
        metavar="{cost-optimization,deep-delegation,orchestrator,manual}",
        help=(
            "Delegation profile for installed Codex instructions. "
            "cost-optimization minimizes parent Codex/GPT-5.5 token usage. "
            "deep-delegation maximizes router offload. "
            "orchestrator keeps Codex/GPT-5.5 in broader control. "
            "manual installs no global automatic delegation. "
            "Only affects --mode default."
        ),
    )
    init.set_defaults(handler=cmd_init)

    service = subcommands.add_parser("install-service", help="write a systemd user service")
    add_common_settings_args(service)
    service.add_argument("--name", default="subagent-router")
    service.add_argument("--force", action="store_true")
    service.set_defaults(handler=cmd_install_service)

    validate = subcommands.add_parser("validate-artifacts", help="validate release artifacts for packaging consistency")
    validate.add_argument("--json", action="store_true", help="print JSON results")
    validate.set_defaults(handler=cmd_validate_artifacts)

    return parser


def add_common_settings_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--log-dir", default=None)
    parser.add_argument("--activity-file", default=None)
    parser.add_argument("--session-mirror-file", default=None)
    parser.add_argument("--provider-error-log-dir", default=None)
    parser.add_argument("--audit-log-file", default=None)
    parser.add_argument("--usage-file", default=None)
    parser.add_argument("--provider", default=None, help="default provider name")
    parser.add_argument("--fallback-providers", default=None, help="comma-separated fallback provider names")
    parser.add_argument("--deepseek-base-url", default=None)
    parser.add_argument("--model", default=None, help="override upstream DeepSeek model")
    parser.add_argument("--openai-compatible-base-url", default=None)
    parser.add_argument("--openai-compatible-api-key", default=None)
    parser.add_argument("--openai-compatible-model", default=None)
    parser.add_argument("--ollama-base-url", default=None)
    parser.add_argument("--ollama-model", default=None)
    parser.add_argument("--ollama-enabled", action="store_true")
    parser.add_argument("--max-cost-per-task", default=None)
    parser.add_argument("--max-tokens-per-task", type=int, default=None)
    parser.add_argument("--budget-mode", choices=("warn", "hard-stop"), default=None)
    parser.add_argument("--provider-allowlist", default=None)
    parser.add_argument("--provider-denylist", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mock", action="store_true", help="use deterministic local mock responses")
    parser.add_argument("--trace", action="store_true", help="print compact proxy trace output")
    parser.add_argument("--allow-apply-patch", action="store_true")
    parser.add_argument("--send-parallel-tool-calls", action="store_true")


def settings_from_args(args: argparse.Namespace, *, ephemeral_port: bool = False) -> Settings:
    env = dict(os.environ)
    arg_env = {
        "SUBAGENT_ROUTER_HOST": args.host,
        "SUBAGENT_ROUTER_PORT": str(args.port) if args.port is not None else None,
        "SUBAGENT_ROUTER_STATE_DIR": args.state_dir,
        "SUBAGENT_ROUTER_LOG_DIR": args.log_dir,
        "SUBAGENT_ROUTER_ACTIVITY_FILE": args.activity_file,
        "SUBAGENT_ROUTER_SESSION_MIRROR_FILE": args.session_mirror_file,
        "SUBAGENT_ROUTER_PROVIDER_ERROR_LOG_DIR": args.provider_error_log_dir,
        "SUBAGENT_ROUTER_AUDIT_LOG_FILE": args.audit_log_file,
        "SUBAGENT_ROUTER_USAGE_FILE": args.usage_file,
        "SUBAGENT_ROUTER_PROVIDER": args.provider,
        "SUBAGENT_ROUTER_FALLBACK_PROVIDERS": args.fallback_providers,
        "DEEPSEEK_BASE_URL": args.deepseek_base_url,
        "DEEPSEEK_MODEL": args.model,
        "OPENAI_COMPAT_BASE_URL": args.openai_compatible_base_url,
        "OPENAI_COMPAT_API_KEY": args.openai_compatible_api_key,
        "OPENAI_COMPAT_MODEL": args.openai_compatible_model,
        "OLLAMA_BASE_URL": args.ollama_base_url,
        "OLLAMA_MODEL": args.ollama_model,
        "SUBAGENT_ROUTER_MAX_COST_PER_TASK": args.max_cost_per_task,
        "SUBAGENT_ROUTER_MAX_TOKENS_PER_TASK": str(args.max_tokens_per_task) if args.max_tokens_per_task is not None else None,
        "SUBAGENT_ROUTER_BUDGET_MODE": args.budget_mode,
        "SUBAGENT_ROUTER_PROVIDER_ALLOWLIST": args.provider_allowlist,
        "SUBAGENT_ROUTER_PROVIDER_DENYLIST": args.provider_denylist,
    }
    for key, value in arg_env.items():
        if value is not None:
            env[key] = value
    if args.mock:
        env["DEEPSEEK_PROXY_MOCK"] = "1"
    if args.trace:
        env["SUBAGENT_ROUTER_TRACE"] = "1"
    if args.allow_apply_patch:
        env["DEEPSEEK_ALLOW_APPLY_PATCH"] = "1"
    if args.send_parallel_tool_calls:
        env["DEEPSEEK_SEND_PARALLEL_TOOL_CALLS"] = "1"
    if args.ollama_enabled:
        env["OLLAMA_ENABLED"] = "1"
    if args.dry_run:
        env["SUBAGENT_ROUTER_DRY_RUN"] = "1"
    if ephemeral_port and args.port is None:
        port = free_loopback_port(args.host or "127.0.0.1")
        env["SUBAGENT_ROUTER_PORT"] = str(port)
    return Settings.from_env(env)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_start(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    if args.background:
        return start_background(args, settings)
    if args.attach_logs:
        print("start: --attach-logs requires --background", file=sys.stderr)
        return 2
    saved = {}
    proxy_env = settings.as_env(include_secrets=True)
    for key, value in proxy_env.items():
        saved[key] = os.environ.get(key)
        os.environ[key] = value
    try:
        proxy_main(settings)
    finally:
        for key in proxy_env:
            if saved[key] is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved[key]
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    return stop_background(settings, timeout=args.timeout, force=args.force, quiet=False)


def cmd_restart(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    stop_background(settings, timeout=args.timeout, force=args.force, quiet=True)
    return start_background(args, settings)


def cmd_status(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    pid = read_pid(settings)
    profile = installed_delegation_profile()
    if pid is None:
        print("Subagent Router is not running.")
        print(f"Delegation Profile: {profile}")
        return 1
    if not process_running(pid):
        pid_file(settings).unlink(missing_ok=True)
        print(f"Subagent Router is not running (removed stale pid {pid}).")
        print(f"Delegation Profile: {profile}")
        return 1
    print(f"Subagent Router is running (pid {pid}) at http://{settings.host}:{settings.port}/v1")
    print(f"Delegation Profile: {profile}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    path = settings.audit_log_file if args.audit else server_log_file(settings)
    if not path.exists():
        kind = "audit log" if args.audit else "server log"
        print(f"No {kind} found at {path}", file=sys.stderr)
        return 1
    return print_log(path, lines=args.lines, follow=args.follow)


def cmd_usage(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    if not settings.usage_file.exists():
        summary = {
            "request_count": 0,
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            "requests_by_provider": {},
            "requests_by_model": {},
        }
    else:
        try:
            summary = json.loads(settings.usage_file.read_text(encoding="utf-8"))
            if not isinstance(summary, dict):
                raise ValueError("usage summary is not an object")
        except (OSError, ValueError, json.JSONDecodeError):
            summary = {
                "request_count": 0,
                "total_tokens": 0,
                "total_cost_usd": 0.0,
                "requests_by_provider": {},
                "requests_by_model": {},
            }
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    print(f"requests: {summary.get('request_count', 0)}")
    print(f"tokens: {summary.get('total_tokens', 0)}")
    print(f"estimated_cost_usd: {summary.get('total_cost_usd', 0.0)}")
    for provider, count in sorted((summary.get("requests_by_provider") or {}).items()):
        print(f"provider.{provider}: {count}")
    for model, count in sorted((summary.get("requests_by_model") or {}).items()):
        print(f"model.{model}: {count}")
    return 0


def cmd_debug_bundle(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    output = Path(args.output).expanduser().resolve() if args.output else settings.state_dir / f"debug-bundle-{int(time.time())}.tar.gz"
    output.parent.mkdir(parents=True, exist_ok=True)
    candidates = [
        settings.activity_file,
        settings.session_mirror_file,
        settings.audit_log_file,
        settings.usage_file,
        server_log_file(settings),
    ]
    with tarfile.open(output, "w:gz") as bundle:
        for path in candidates:
            if path.exists() and path.is_file():
                bundle.add(path, arcname=path.name)
    print(f"Wrote {output}")
    return 0


def cmd_stdio(args: argparse.Namespace) -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    if not isinstance(payload, dict):
        print("stdio: expected a JSON object", file=sys.stderr)
        return 2
    settings = settings_from_args(args)
    try:
        response = process_handoff_payload(settings, payload)
    except Exception as exc:
        print(f"stdio: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(response, sort_keys=True))
    return 0


def process_handoff_payload(settings: Settings, payload: dict) -> dict:
    import subagent_router.app as proxy_app
    from subagent_router.normalization import PayloadNormalizationError, normalize_request

    old_settings = proxy_app.SETTINGS
    proxy_app.SETTINGS = settings
    try:
        normalized = normalize_request(
            payload,
            allow_apply_patch_enabled=settings.apply_patch_override(),
        )
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        route = proxy_app.RouteSelection(
            str(metadata.get("provider") or settings.provider),
            str(metadata["model"]) if metadata.get("model") else None,
            settings.fallback_providers,
            str(metadata.get("routing_policy") or metadata.get("policy") or "handoff"),
            "handoff request",
        )
        provider_result = (
            proxy_app.dry_run_provider_response(normalized, route)
            if settings.dry_run or payload.get("dry_run") is True
            else asyncio.run(proxy_app.call_provider(normalized, route))
        )
        return proxy_app.responses_object(
            payload,
            normalized,
            provider_result.chat_response,
            provider_result=provider_result,
            route=route,
        )
    except PayloadNormalizationError:
        raise
    finally:
        proxy_app.SETTINGS = old_settings


def cmd_handoff(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    input_dir = Path(args.input_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else input_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    while True:
        processed = process_handoff_files(settings, input_dir, output_dir)
        if args.once:
            return 0 if processed >= 0 else 1
        time.sleep(args.poll_interval)


def process_handoff_files(settings: Settings, input_dir: Path, output_dir: Path) -> int:
    count = 0
    for path in sorted(input_dir.glob("*.json")):
        if path.name.endswith(".response.json") or path.name.endswith(".error.json"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("task file must contain a JSON object")
            result = process_handoff_payload(settings, payload)
            (output_dir / f"{path.stem}.response.json").write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            (output_dir / f"{path.stem}.error.json").write_text(
                json.dumps({"error": str(exc)}, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        count += 1
    return count


def cmd_tui(args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.panel import Panel
    from rich.layout import Layout
    from rich import box
    import httpx

    settings = settings_from_args(args)
    console = Console()
    base_url = f"http://{settings.host}:{settings.port}"
    codex_home = Path(args.codex_home).expanduser().resolve()
    proxy_url = f"{base_url}/v1"

    def get_remote_config():
        try:
            resp = httpx.get(f"{base_url}/v1/config", timeout=1.0)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None

    def patch_remote_config(updates):
        try:
            httpx.patch(f"{base_url}/v1/config", json=updates, timeout=2.0)
            return True
        except Exception:
            return False

    def post_remote_reset():
        try:
            httpx.post(f"{base_url}/v1/reset", timeout=2.0)
            return True
        except Exception:
            return False

    def check_remote_provider(name):
        try:
            resp = httpx.get(f"{base_url}/v1/check-provider/{name}", timeout=6.0)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {"available": False, "error": "Router unreachable"}

    def apply_model_assignment(pname, new_model, role):
        if role == "default":
            return patch_remote_config({"provider_models": {pname: new_model}})
        elif role == "worker":
            # Set provider-specific worker model and clear any global override
            return patch_remote_config({
                "provider_roles": {pname: {"worker": new_model}},
                "routing_policies": {"safe-default": {"model": None}}
            })
        elif role == "explorer":
            return patch_remote_config({
                "provider_roles": {pname: {"explorer": new_model}},
            })
        elif role == "reviewer":
            # Set provider-specific reviewer model and clear any global override
            return patch_remote_config({
                "provider_roles": {pname: {"reviewer": new_model}},
                "routing_policies": {"cheap-review": {"model": None}}
            })
        return False

    def _agent_type_from_entry(entry):
        agent_type = entry.get("agent_type")
        if agent_type:
            return str(agent_type)
        model = str(entry.get("requested_model") or entry.get("model") or "")
        for candidate in ("explorer", "worker", "reviewer"):
            if model in (f"subagent-router-{candidate}", f"deepseek-{candidate}"):
                return candidate
        return ""

    def current_delegation_profile(remote_config=None):
        installed = installed_delegation_profile(codex_home)
        if installed != DEFAULT_PROFILE:
            return installed
        raw_profile = (remote_config or {}).get("delegation_profile")
        if raw_profile:
            try:
                return normalize_profile(raw_profile)
            except ValueError:
                pass
        return installed

    def switch_delegation_profile(profile):
        ok, normalized, detail = _switch_delegation_profile(
            codex_home,
            proxy_url,
            profile,
            force=False,
        )
        if not ok and detail:
            return False, f"[red]Profile switch failed: {detail}[/red]"
        if not ok:
            return (
                False,
                (
                    f"[yellow]Profile unchanged; custom instructions preserved. "
                    f"Run init --profile {normalized} --force to overwrite.[/yellow]"
                ),
            )
        return True, f"[green]Delegation profile → {normalized}; init completed.[/green]"

    def generate_dashboard(remote_config=None) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="footer", size=3),
        )

        # Header
        layout["header"].update(Panel(f"[bold cyan]Subagent Router Control Panel[/bold cyan] | Host: [dim]{settings.host}:{settings.port}[/dim]", box=box.ROUNDED))

        # Main Content
        main_layout = Layout()
        main_layout.split_column(
            Layout(name="stats", size=10),
            Layout(name="activity"),
        )

        main_table = Table.grid(expand=True)
        main_table.add_column(ratio=1)
        main_table.add_column(ratio=1)

        # Config Panel
        current_provider = remote_config.get("provider") if remote_config else settings.provider
        current_mode = remote_config.get("budget_mode") if remote_config else settings.budget_mode
        delegation_profile = current_delegation_profile(remote_config)
        config_info = Table(show_header=False, box=None)
        config_info.add_row("[bold]Status:[/bold]", "[green]UP[/green]" if remote_config else "[red]DOWN[/red]")
        config_info.add_row("[bold]Provider:[/bold]", f"[magenta]{current_provider}[/magenta]")
        config_info.add_row("[bold]Delegation Profile:[/bold]", f"[cyan]{delegation_profile}[/cyan]")
        config_info.add_row("[bold]Budget Mode:[/bold]", f"[yellow]{current_mode}[/yellow]")
        config_info.add_row("[bold]Fallbacks:[/bold]", ", ".join(settings.fallback_providers) or "none")

        # Budget Panel
        budget_table = Table(show_header=False, box=None)
        if settings.usage_file.exists():
            try:
                import datetime
                usage = json.loads(settings.usage_file.read_text(encoding="utf-8"))
                today = datetime.date.today().isoformat()
                day_data = usage.get("daily_usage", {}).get(today, {})
                daily_cost = float(day_data.get("total_cost_usd", 0.0))
                daily_tokens = int(day_data.get("total_tokens", 0))

                max_cost = remote_config.get("max_cost_per_day") if remote_config else settings.max_cost_per_day

                cost_color = "green"
                if max_cost and daily_cost >= max_cost: cost_color = "red"
                elif max_cost and daily_cost >= max_cost * 0.8: cost_color = "yellow"

                budget_table.add_row("[bold]Daily USD:[/bold]", f"[{cost_color}]{daily_cost:>8.4f}$[/{cost_color}] / {max_cost or 0:.4f}$")
                budget_table.add_row("[bold]Daily Toks:[/bold]", f"{daily_tokens:>8} / {settings.max_tokens_per_day or 'unlimited'}")
            except Exception:
                budget_table.add_row("[yellow](budget error)[/yellow]")
        else:
            budget_table.add_row("[dim]No usage data[/dim]")

        main_table.add_row(
            Panel(config_info, title="System Configuration", border_style="cyan"),
            Panel(budget_table, title="Budget Status", border_style="yellow")
        )
        main_layout["stats"].update(main_table)

        # Activity Panel
        activity_table = Table(box=None, expand=True)
        activity_table.add_column("Time", style="dim")
        activity_table.add_column("Status")
        activity_table.add_column("Provider")
        activity_table.add_column("Agent")
        activity_table.add_column("Toks (in/cache/out)", justify="right")
        activity_table.add_column("Cost", justify="right")

        if settings.audit_log_file.exists():
            try:
                with settings.audit_log_file.open("rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 4096))
                    tail = f.read().decode("utf-8", errors="replace")
                lines = [l for l in tail.strip().split("\n") if l.strip()]
                for al in lines[-5:][::-1]:
                    entry = json.loads(al)
                    raw_ts = entry.get("timestamp")
                    ts = datetime.datetime.fromtimestamp(raw_ts).strftime("%H:%M:%S") if isinstance(raw_ts, (int, float)) else "??:??:??"
                    status = "[green]OK[/green]" if entry.get("status") in ("success", "ok") else "[red]ERR[/red]"

                    usage = entry.get("usage") or {}
                    t_in = usage.get("prompt_tokens", usage.get("input_tokens", 0))
                    t_out = usage.get("completion_tokens", usage.get("output_tokens", 0))
                    t_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
                    t_cached = t_details.get("cached_tokens", 0)
                    if t_in > 0 or t_out > 0:
                        t_str = f"[cyan]{t_in}[/cyan] / [green]{t_cached}[/green] / [magenta]{t_out}[/magenta]"
                    else:
                        t_str = ""

                    activity_table.add_row(ts, status, entry.get("provider", "???"), _agent_type_from_entry(entry), t_str, f"${entry.get('estimated_cost_usd', 0.0):.4f}")
            except Exception:
                activity_table.add_row("", "[yellow]Audit log error[/yellow]", "", "", "", "")
        else:
            activity_table.add_row("", "[dim]No activity yet[/dim]", "", "", "", "")

        main_layout["activity"].update(Panel(activity_table, title="Recent Requests", border_style="magenta"))
        layout["main"].update(main_layout)

        # Footer
        layout["footer"].update(Panel(r"[bold]Actions:[/bold] [white]\[P] Provider | \[B] Budget | \[M] Mode | \[R] Reset | \[Q] Quit[/white]", border_style="white"))

        return layout

    from threading import Thread, Event
    import queue

    input_queue = queue.Queue()
    stop_event = Event()
    is_tty = sys.stdin.isatty() and sys.stdout.isatty()

    def read_tui_key() -> str | None:
        ch = sys.stdin.read(1)
        if not ch:
            return None
        if ch != "\x1b":
            return ch
        # Non-blocking read for escape sequence continuation
        if not select.select([sys.stdin], [], [], 0.3)[0]:
            return "\x1b"
        second = sys.stdin.read(1)
        if second != "[":
            return "\x1b"
        if not select.select([sys.stdin], [], [], 0.1)[0]:
            return "\x1b"
        third = sys.stdin.read(1)
        if third == "A":
            return "up"
        if third == "B":
            return "down"
        if third == "5":
            if select.select([sys.stdin], [], [], 0.1)[0]:
                sys.stdin.read(1)
            return "page_up"
        if third == "6":
            if select.select([sys.stdin], [], [], 0.1)[0]:
                sys.stdin.read(1)
            return "page_down"
        return None

    def input_thread_func():
        import sys, tty, termios
        if not is_tty:
            return

        try:
            fd = sys.stdin.fileno()
        except (AttributeError, io.UnsupportedOperation):
            return

        try:
            old_settings = termios.tcgetattr(fd)
        except termios.error:
            return

        try:
            # cbreak mode: single-key input (no line buffering, no echo)
            # but PRESERVES output processing so Rich can render properly.
            # (setraw would disable ONLCR, breaking \n → \r\n translation
            # and garbling all Rich layout output)
            tty.setcbreak(fd)
            while not stop_event.is_set():
                ch = read_tui_key()
                if not ch:
                    break
                input_queue.put(ch)
        except Exception:
            pass
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except Exception:
                pass

    # TUI State
    class TUIState:
        def __init__(self):
            self.menu = "main"
            self.last_config = get_remote_config()
            self.last_refresh = 0
            self.message = ""
            self.message_expiry = 0
            self.needs_update = True
            # Budget input state
            self.budget_field = None      # which field is being edited
            self.budget_input = ""        # accumulated digits
            self.budget_field_key = None  # API key for PATCH
            # Provider health and models
            self.provider_health = {}     # provider name -> availability result
            self.available_models = {}   # provider name -> list of models
            self.model_menu_provider = None
            self.model_menu_options = []
            self.model_input = ""
            self.model_menu_role = "default"  # "default", "worker", "reviewer"
            # Cost input state
            self.cost_provider = None
            self.cost_model = None
            self.cost_field = None
            self.cost_menu_options = []
            self.activity_offset = 0

    state = TUIState()

    # Initial health check for current provider
    cp = (state.last_config.get("provider") if state.last_config else settings.provider)
    def initial_check(pname):
        state.provider_health[pname] = check_remote_provider(pname)
        state.needs_update = True
    Thread(target=initial_check, args=(cp,), daemon=True).start()

    # Budget menu field definitions
    BUDGET_FIELDS = [
        ("1", "Daily Cost Limit ($)",      "max_cost_per_day",      "float"),
        ("2", "Daily Token Limit",         "max_tokens_per_day",    "int"),
        ("3", "Session Cost Limit ($)",    "max_cost_per_session",  "float"),
        ("4", "Session Token Limit",       "max_tokens_per_session","int"),
        ("5", "Per-Task Cost Limit ($)",   "max_cost_per_task",     "float"),
        ("6", "Per-Task Token Limit",      "max_tokens_per_task",   "int"),
    ]

    def build_dashboard_content():
        """Build the inner content renderables for the dashboard panels."""
        import datetime as dt
        remote = state.last_config
        current_provider = remote.get("provider") if remote else settings.provider
        current_mode = remote.get("budget_mode") if remote else settings.budget_mode
        delegation_profile = current_delegation_profile(remote)

        cfg = remote or {}

        # Config info
        config_info = Table(show_header=False, box=None, padding=(0, 1))
        config_info.add_row("[bold]Status:[/bold]", "[green]UP[/green]" if remote else "[red]DOWN[/red]")

        health = state.provider_health.get(current_provider, {})
        if health:
            avail = "[green]Online[/green]" if health.get("available") else f"[red]Offline[/red]"
            config_info.add_row("[bold]Health:[/bold]", avail)

        config_info.add_row("[bold]Provider:[/bold]", f"[magenta]{current_provider}[/magenta]")
        config_info.add_row("[bold]Delegation Profile:[/bold]", f"[cyan]{delegation_profile}[/cyan]")
        model_name = "unknown"
        if remote:
            model_name = remote.get('providers', {}).get(current_provider, {}).get('model')
        if not model_name:
            provider_cfg = settings.providers.get(current_provider)
            model_name = provider_cfg.model if provider_cfg else 'unknown'

        config_info.add_row("[bold]Model:[/bold]", f"[cyan]{model_name}[/cyan]")
        
        # Routing policies and role assignments.
        policies = cfg.get("routing_policies", {})
        pricing = remote.get("provider_pricing", {}) if remote else {}
        cur_pricing = pricing.get(current_provider, {})

        for role_name, policy_key, model_key, default_alias in [
            ("Explorer", None, "explorer_model", "deepseek-v4-flash"),
            ("Worker", "safe-default", "worker_model", "deepseek-v4-flash"),
            ("Reviewer", "cheap-review", "reviewer_model", "deepseek-v4-pro"),
        ]:
            policy = policies.get(policy_key, {}) if policy_key else {}
            p_provider = policy.get("provider")
            p_model = policy.get("model")
            
            # If no global policy, check current provider's specific role assignment
            if not p_provider and not p_model:
                p_model = cur_pricing.get(model_key)
                if p_model:
                    p_provider = current_provider

            p_val = f"[cyan]{p_model or default_alias}[/cyan]"
            if p_provider:
                p_val += f" [dim]on {p_provider}[/dim]"
            config_info.add_row(f"[bold]{role_name}:[/bold]", p_val)

        config_info.add_row("[bold]Budget Mode:[/bold]", f"[yellow]{current_mode}[/yellow]")
        config_info.add_row("[bold]Fallbacks:[/bold]", f"[dim]{', '.join(settings.fallback_providers) or 'none'}[/dim]")

        # Show API pricing per provider in config panel
        pricing = cfg.get("provider_pricing") or {}
        if pricing:
            config_info.add_row("", "")  # spacer
            for pname, pdata in pricing.items():
                active_model = str(pdata.get("model") or "default")
                
                # Provider header
                config_info.add_row(f"[bold]{pname}:[/bold]", "")
                
                # Active model row
                inp = pdata.get("input_cost_per_million")
                out = pdata.get("output_cost_per_million")
                cached = pdata.get("cached_input_cost_per_million")
                hint = pdata.get("cost_hint", "")
                
                if inp is not None or out is not None:
                    rate_str = f"[dim]in[/dim] ${inp or 0:.4f}  [dim]hit[/dim] ${cached or 0:.4f}  [dim]out[/dim] ${out or 0:.4f}"
                elif hint == "zero-api-cost":
                    rate_str = "[green]free (local)[/green]"
                else:
                    rate_str = "[dim]unknown[/dim]"
                
                config_info.add_row(f"  [bold cyan]* {active_model}[/bold cyan]:", rate_str)
                
                # Other model-specific overrides
                overrides = pdata.get("model_overrides", {})
                for mname, mrates in overrides.items():
                    mname_str = str(mname or "")
                    if mname_str == active_model:
                        continue
                    m_inp = mrates.get("in") or 0
                    m_hit = mrates.get("cached") or 0
                    m_out = mrates.get("out") or 0
                    m_rate_str = f"[dim]in[/dim] ${m_inp:.4f}  [dim]hit[/dim] ${m_hit:.4f}  [dim]out[/dim] ${m_out:.4f}"
                    config_info.add_row(f"  [dim]→ {mname_str}:[/dim]", m_rate_str)

        # Budget info — comprehensive view
        budget_info = Table(show_header=False, box=None, padding=(0, 1))

        def _fmt_cost(v):
            return f"${v:.4f}" if v is not None else "[dim]none[/dim]"
        def _fmt_tokens(v):
            return f"{v:,}" if v is not None else "[dim]none[/dim]"
        def _usage_counts(usage):
            usage = usage if isinstance(usage, dict) else {}
            details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
            if not isinstance(details, dict):
                details = {}
            cached = int(
                usage.get("prompt_cache_hit_tokens")
                or usage.get("input_cache_hit_tokens")
                or details.get("cached_tokens")
                or 0
            )
            input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or cached)
            output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or input_tokens + output_tokens)
            return {
                "input": input_tokens,
                "cached": min(input_tokens, cached),
                "output": output_tokens,
                "total": total_tokens,
            }
        def _daily_token_counts(usage, day_data):
            counts = {
                "input": int(day_data.get("input_tokens") or 0),
                "cached": int(day_data.get("cached_input_tokens") or 0),
                "output": int(day_data.get("output_tokens") or 0),
                "total": int(day_data.get("total_tokens") or 0),
            }
            if counts["input"] or counts["cached"] or counts["output"]:
                return counts
            today = dt.date.today().isoformat()
            for record in usage.get("records") or []:
                if not isinstance(record, dict):
                    continue
                raw_ts = record.get("timestamp")
                if not isinstance(raw_ts, (int, float)):
                    continue
                if dt.datetime.fromtimestamp(raw_ts).date().isoformat() != today:
                    continue
                rc = _usage_counts(record.get("usage"))
                counts["input"] += rc["input"]
                counts["cached"] += rc["cached"]
                counts["output"] += rc["output"]
                counts["total"] += rc["total"]
            return counts
        def _provider_message_from_value(value):
            if isinstance(value, dict):
                error = value.get("error")
                if isinstance(error, dict) and error.get("message"):
                    return str(error["message"])
                if value.get("message"):
                    return str(value["message"])
                return ""
            if not isinstance(value, str):
                return ""
            text = value.strip()
            prefix = "provider rejected request:"
            payload = text[len(prefix):].strip() if text.startswith(prefix) else text
            if not payload.startswith(("{", "[")):
                return ""
            for parser in (json.loads, ast.literal_eval):
                try:
                    parsed = parser(payload)
                except (ValueError, SyntaxError, TypeError):
                    continue
                return _provider_message_from_value(parsed)
            return ""
        def _error_detail(entry):
            for key in ("message", "error", "reason"):
                value = entry.get(key)
                if value:
                    nested = _provider_message_from_value(value)
                    return nested or str(value)
            chain = entry.get("fallback_chain") or []
            for attempt in reversed(chain):
                if not isinstance(attempt, dict):
                    continue
                nested = _provider_message_from_value(attempt.get("error"))
                if nested:
                    return nested
                parts = [
                    str(attempt.get("status_code") or "").strip(),
                    str(attempt.get("error_category") or "").strip(),
                    str(attempt.get("error") or "").strip(),
                ]
                detail = " ".join(part for part in parts if part)
                if detail:
                    return detail
            return ""
        def _activity_status(entry):
            status = str(entry.get("status") or "")
            if status in {"success", "ok"}:
                return "[green]OK[/green]", ""
            if status == "incomplete-output-retry":
                return "[yellow]RETRY[/yellow]", str(entry.get("reason") or "provider output incomplete; retrying")
            if status == "synthetic-continuation":
                tool = entry.get("tool")
                detail = str(entry.get("reason") or "continuing subagent turn")
                if tool:
                    detail = f"{detail}; tool={tool}"
                return "[blue]CONT[/blue]", detail
            return "[red]ERR[/red]", _error_detail(entry)
        def _clip(value, limit=72):
            text = " ".join(str(value).split())
            return text if len(text) <= limit else text[: limit - 1] + "..."

        budget_info.add_row("[bold]Daily $:[/bold]",   _fmt_cost(cfg.get("max_cost_per_day") or settings.max_cost_per_day))
        budget_info.add_row("[bold]Daily Tok:[/bold]",  _fmt_tokens(cfg.get("max_tokens_per_day") or settings.max_tokens_per_day))
        budget_info.add_row("[bold]Session $:[/bold]", _fmt_cost(cfg.get("max_cost_per_session") or settings.max_cost_per_session))
        budget_info.add_row("[bold]Task $:[/bold]",    _fmt_cost(cfg.get("max_cost_per_task") or settings.max_cost_per_task))

        # Per-provider cost limits
        prov_costs = cfg.get("max_cost_per_provider") or settings.max_cost_per_provider or {}
        if prov_costs:
            for pname, pcost in prov_costs.items():
                budget_info.add_row(f"[bold]{pname} cap:[/bold]", _fmt_cost(pcost))

        if settings.usage_file.exists():
            try:
                usage = json.loads(settings.usage_file.read_text(encoding="utf-8"))
                today = dt.date.today().isoformat()
                day_data = usage.get("daily_usage", {}).get(today, {})
                daily_cost = float(day_data.get("total_cost_usd", 0.0))
                daily_tokens = _daily_token_counts(usage, day_data)
                max_cost = cfg.get("max_cost_per_day") or settings.max_cost_per_day
                cost_color = "green"
                if max_cost and daily_cost >= max_cost: cost_color = "red"
                elif max_cost and daily_cost >= max_cost * 0.8: cost_color = "yellow"
                budget_info.add_row("", "")  # spacer
                budget_info.add_row("[bold]Today $:[/bold]", f"[{cost_color}]{daily_cost:.4f}[/{cost_color}]")
                budget_info.add_row(
                    "[bold]Today Tok:[/bold]",
                    (
                        f"[cyan]{daily_tokens['input']:,}[/cyan] / "
                        f"[green]{daily_tokens['cached']:,}[/green] / "
                        f"[magenta]{daily_tokens['output']:,}[/magenta] "
                        f"[dim](total {daily_tokens['total']:,})[/dim]"
                    ),
                )
            except Exception:
                pass

        # Activity table
        activity_table = Table(box=None, expand=True, header_style="bold magenta")
        activity_table.add_column("Time", style="dim", width=10)
        activity_table.add_column("Status", width=28)
        activity_table.add_column("Provider", width=15)
        activity_table.add_column("Agent", width=10)
        activity_table.add_column("Model", width=25)
        activity_table.add_column("Toks (in/cache/out)", justify="right")
        activity_table.add_column("Req Cost", justify="right")

        if settings.audit_log_file.exists():
            try:
                with settings.audit_log_file.open("rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 256 * 1024))
                    tail = f.read().decode("utf-8", errors="replace")
                lines = [l for l in tail.strip().split("\n") if l.strip()]
                visible_rows = 5
                max_offset = max(0, len(lines) - visible_rows)
                state.activity_offset = min(max(state.activity_offset, 0), max_offset)
                end = len(lines) - state.activity_offset
                start = max(0, end - visible_rows)
                for al in lines[start:end][::-1]:
                    entry = json.loads(al)
                    raw_ts = entry.get("timestamp")
                    ts = dt.datetime.fromtimestamp(raw_ts).strftime("%H:%M:%S") if isinstance(raw_ts, (int, float)) else "??:??:??"
                    st, detail = _activity_status(entry)
                    if detail:
                        st = f"{st} {_clip(detail, 48)}"

                    try:
                        counts = _usage_counts(entry.get("usage"))
                    except (ValueError, TypeError):
                        counts = {"input": 0, "cached": 0, "output": 0}

                    if counts["input"] > 0 or counts["output"] > 0:
                        t_str = f"[cyan]{counts['input']}[/cyan] / [green]{counts['cached']}[/green] / [magenta]{counts['output']}[/magenta]"
                    else:
                        t_str = ""

                    activity_table.add_row(
                        ts,
                        st,
                        entry.get("provider", "???"),
                        _agent_type_from_entry(entry),
                        str(entry.get("model", "???")),
                        t_str,
                        f"${entry.get('estimated_cost_usd', 0.0):.4f}",
                    )
            except Exception:
                activity_table.add_row("", "[yellow]Audit log error[/yellow]", "", "", "", "", "")
        else:
            activity_table.add_row("", "[dim]No activity yet[/dim]", "", "", "", "", "")

        # Footer text — context-sensitive
        if state.menu == "provider":
            providers = sorted(remote.get("providers", {}).keys()) if remote else ["deepseek", "groq", "ollama"]
            opts = []
            for i, p in enumerate(providers[:9]):
                opts.append(f"[{i+1}] {p.capitalize()}")
            footer_text = "[bold yellow]Switch Provider:[/bold yellow] " + "  ".join(opts) + "  [Any] Back"
        elif state.menu == "profile":
            current = current_delegation_profile(remote)
            opts = []
            for i, profile in enumerate(CANONICAL_PROFILES[:9]):
                marker = " *" if profile == current else ""
                opts.append(f"[{i+1}] {profile}{marker}")
            footer_text = "[bold yellow]Switch Delegation Profile:[/bold yellow] " + "  ".join(opts) + "  [0] Back"
        elif state.menu == "model_role":
            footer_text = "[bold yellow]Assign Model to Role:[/bold yellow] [1] Global Default  [2] Explorer  [3] Worker  [4] Reviewer  [Any] Back"
        elif state.menu == "model":
            footer_text = f"[bold yellow]{state.model_menu_role.upper()} model for {state.model_menu_provider}:[/bold yellow] "
            opts = []
            for i, opt in enumerate(state.model_menu_options[:9]):
                opts.append(f"[{i+1}] {opt}")
            footer_text += "  ".join(opts) + r"  [white]\[M] Manual[/white]  [Any] Back"
        elif state.menu == "model_input":
            footer_text = f"[bold cyan]Enter model for {state.model_menu_provider}:[/bold cyan] [white]{state.model_input}▌[/white]  (Enter=Apply  Esc=Cancel)"
        elif state.menu == "budget":
            lines_parts = ["[bold yellow]Budget Config:[/bold yellow]"]
            for key, label, api_key, ftype in BUDGET_FIELDS:
                lines_parts.append(f"[{key}] {label}")
            lines_parts.append("[0] Back")
            footer_text = "  ".join(lines_parts)
        elif state.menu == "budget_input":
            footer_text = f"[bold cyan]Set {state.budget_field}:[/bold cyan] [white]{state.budget_input}▌[/white]  (Enter=Apply  Esc/0=Cancel)"
        elif state.menu == "cost_provider":
            providers = sorted(remote.get("providers", {}).keys()) if remote else ["deepseek", "groq", "ollama"]
            opts = []
            for i, p in enumerate(providers[:9]):
                opts.append(f"[{i+1}] {p.capitalize()}")
            footer_text = "[bold yellow]Select Provider to Edit Costs:[/bold yellow] " + "  ".join(opts) + "  [0] Back"
        elif state.menu == "cost_model":
            footer_text = f"[bold yellow]Select Model for {state.cost_provider}:[/bold yellow] "
            opts = []
            for i, opt in enumerate(state.cost_menu_options[:9]):
                opts.append(f"[{i+1}] {opt}")
            footer_text += "  ".join(opts) + "  [0] Back"
        elif state.menu == "cost_field":
            target = f"{state.cost_provider} ({state.cost_model})" if state.cost_model else state.cost_provider
            footer_text = f"[bold yellow]Edit {target} Rates:[/bold yellow] [1] Input  [2] Output  [3] Cached  [0] Back"
        elif state.menu == "cost_input":
            target = f"{state.cost_provider} {state.cost_model}" if state.cost_model else state.cost_provider
            footer_text = f"[bold cyan]Set {target} {state.cost_field} cost/1M:[/bold cyan] [white]${state.budget_input}▌[/white]  (Enter=Apply  Esc/0=Cancel)"
        elif state.message:
            footer_text = state.message
        else:
            footer_text = r"[bold]Actions:[/bold]  ↑ older  ↓ newer  PgUp/PgDn page  \[P] Provider  \[D] Profile  \[L] Model  \[C] Cost  \[B] Budget  \[M] Mode  \[R] Reset Usage  \[X] Clear Config  \[Q] Quit"

        return config_info, budget_info, activity_table, footer_text

    def make_layout():
        """Create a Layout that fills exactly one terminal screen."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="stats", ratio=2),
            Layout(name="activity", ratio=1),
            Layout(name="footer", size=3),
        )
        return layout

    def update_layout(layout):
        """Populate the layout panels with current data."""
        config_info, budget_info, activity_table, footer_text = build_dashboard_content()

        layout["header"].update(Panel(
            f"[bold cyan]Subagent Router[/bold cyan] | [dim]{settings.host}:{settings.port}[/dim]",
            box=box.ROUNDED, border_style="cyan"
        ))

        stats_grid = Table.grid(expand=True)
        stats_grid.add_column(ratio=1)
        stats_grid.add_column(ratio=1)
        stats_grid.add_row(
            Panel(config_info, title="[cyan]Configuration[/cyan]", border_style="cyan"),
            Panel(budget_info, title="[yellow]Budgets[/yellow]", border_style="yellow"),
        )
        layout["stats"].update(stats_grid)
        layout["activity"].update(Panel(activity_table, title="[magenta]Recent Requests[/magenta]", border_style="magenta"))
        layout["footer"].update(Panel(footer_text, border_style="white"))

    # ── Non-TTY / Test Mode ──
    if not is_tty:
        watch = getattr(args, "watch", False)
        if watch:
            try:
                while True:
                    ly = make_layout()
                    update_layout(ly)
                    console.print(ly)
                    time.sleep(2)
                    state.last_config = get_remote_config()
            except KeyboardInterrupt:
                return 130
        else:
            ly = make_layout()
            update_layout(ly)
            console.print(ly)
            return 0

    # ── Interactive TTY Mode ──
    it = Thread(target=input_thread_func, daemon=True)
    it.start()

    layout = make_layout()
    update_layout(layout)

    try:
        with Live(layout, console=console, screen=True, refresh_per_second=1) as live:
            while not stop_event.is_set():
                now = time.time()

                if now - state.last_refresh > 3:
                    state.last_config = get_remote_config()
                    state.last_refresh = now
                    state.needs_update = True

                if state.message and now > state.message_expiry:
                    state.message = ""
                    state.needs_update = True

                # Process Input
                try:
                    choice = input_queue.get_nowait()
                    state.needs_update = True
                    ch = choice.lower() if state.menu != "budget_input" else choice

                    if state.menu == "main":
                        if ch == 'q' or ch == '\x03':
                            stop_event.set()
                        elif ch in {"up", "page_up", "k"}:
                            state.activity_offset += 5 if ch == "page_up" else 1
                        elif ch in {"down", "page_down", "j"}:
                            step = 5 if ch == "page_down" else 1
                            state.activity_offset = max(0, state.activity_offset - step)
                        elif ch == 'p':
                            state.menu = "provider"
                        elif ch == 'd':
                            state.menu = "profile"
                        elif ch == 'b':
                            state.menu = "budget"
                        elif ch == 'c':
                            state.menu = "cost_provider"
                        elif ch == 'l':
                            state.menu = "model_role"
                        elif ch == 'r':
                            if post_remote_reset():
                                state.message = "[green]Usage and budget state reset successfully.[/green]"
                            else:
                                state.message = "[red]Failed to reset (server unreachable)[/red]"
                            state.message_expiry = now + 3
                        elif ch == 'm':
                            cur = (state.last_config.get("budget_mode") if state.last_config else settings.budget_mode)
                            new_mode = "hard-stop" if cur == "warn" else "warn"
                            if patch_remote_config({"budget_mode": new_mode}):
                                state.last_config = get_remote_config()
                                state.message = f"[green]Mode → {new_mode}[/green]"
                                state.message_expiry = now + 2
                        elif ch == 'x':
                            # Reset all config overrides (models and roles)
                            updates = {
                                "provider_models": {p: None for p in settings.providers},
                                "routing_policies": {
                                    "safe-default": {"provider": None, "model": None},
                                    "cheap-review": {"provider": None, "model": None}
                                }
                            }
                            if patch_remote_config(updates):
                                state.last_config = get_remote_config()
                                state.message = "[green]Configuration overrides cleared.[/green]"
                                state.message_expiry = now + 3
                            else:
                                state.message = "[red]Failed to reset config[/red]"
                                state.message_expiry = now + 3

                    elif state.menu == "model_role":
                        role_map = {"1": "default", "2": "explorer", "3": "worker", "4": "reviewer"}
                        if ch in role_map:
                            state.model_menu_role = role_map[ch]
                            # Start fetching
                            remote = state.last_config or {}
                            cp = remote.get("provider") or settings.provider
                            state.model_menu_provider = cp
                            state.message = f"[dim]Fetching models for {cp}...[/dim]"
                            state.message_expiry = now + 10
                            state.needs_update = True

                            def fetch_models(pname):
                                res = check_remote_provider(pname)
                                state.available_models[pname] = res.get("models", [])
                                state.provider_health[pname] = res
                                state.model_menu_options = res.get("models", [])
                                state.menu = "model"
                                state.message = ""
                                state.needs_update = True

                            Thread(target=fetch_models, args=(cp,), daemon=True).start()
                        else:
                            state.menu = "main"

                    elif state.menu == "provider":
                        providers = sorted(state.last_config.get("providers", {}).keys()) if state.last_config else ["deepseek", "groq", "ollama"]
                        if ch.isdigit() and 1 <= int(ch) <= len(providers):
                            new_p = providers[int(ch) - 1]
                            if patch_remote_config({"provider": new_p}):
                                state.last_config = get_remote_config()
                                state.message = f"[green]Provider → {new_p}[/green] [dim](checking health...)[/dim]"
                                state.message_expiry = now + 5

                                def check_new_p(pname):
                                    res = check_remote_provider(pname)
                                    state.provider_health[pname] = res
                                    if res.get("available"):
                                        state.message = f"[green]Provider → {pname} (Online)[/green]"
                                    else:
                                        err = res.get("error", "offline")
                                        state.message = f"[red]Provider → {pname} (Error: {err})[/red]"
                                    state.message_expiry = now + 3
                                    state.needs_update = True

                                Thread(target=check_new_p, args=(new_p,), daemon=True).start()
                        state.menu = "main"

                    elif state.menu == "profile":
                        if ch == '0' or ch == '\x1b':
                            state.menu = "main"
                        elif ch.isdigit() and 1 <= int(ch) <= len(CANONICAL_PROFILES):
                            new_profile = CANONICAL_PROFILES[int(ch) - 1]
                            ok, message = switch_delegation_profile(new_profile)
                            if ok:
                                state.last_config = get_remote_config()
                            state.message = message
                            state.message_expiry = now + 5
                            state.menu = "main"
                        else:
                            state.menu = "main"

                    elif state.menu == "model":
                        if ch.isdigit() and 1 <= int(ch) <= len(state.model_menu_options):
                            new_model = state.model_menu_options[int(ch) - 1]
                            pname = state.model_menu_provider
                            if apply_model_assignment(pname, new_model, state.model_menu_role):
                                state.last_config = get_remote_config()
                                state.message = f"[green]{state.model_menu_role} model → {new_model}[/green]"
                                state.message_expiry = now + 2
                            state.menu = "main"
                        elif ch == 'm':
                            state.menu = "model_input"
                            state.model_input = ""
                        else:
                            state.menu = "main"

                    elif state.menu == "model_input":
                        if choice == '\x7f' or choice == '\x08':  # backspace
                            state.model_input = state.model_input[:-1]
                        elif choice == '\r' or choice == '\n':  # enter
                            if state.model_input:
                                pname = state.model_menu_provider
                                new_model = state.model_input
                                if apply_model_assignment(pname, new_model, state.model_menu_role):
                                    state.last_config = get_remote_config()
                                    state.message = f"[green]{state.model_menu_role} model → {new_model}[/green]"
                                    state.message_expiry = now + 2
                            state.menu = "main"
                        elif choice == '\x1b':  # escape
                            state.menu = "model"
                        elif len(choice) == 1 and ord(choice) >= 32:
                            state.model_input += choice

                    elif state.menu == "budget":
                        if ch == '0' or ch == '\x1b':
                            state.menu = "main"
                        else:
                            for key, label, api_key, ftype in BUDGET_FIELDS:
                                if ch == key:
                                    state.menu = "budget_input"
                                    state.budget_field = label
                                    state.budget_field_key = api_key
                                    state.budget_input = ""
                                    break
                            else:
                                state.menu = "main"

                    elif state.menu == "budget_input":
                        if choice in '0123456789.':
                            state.budget_input += choice
                        elif choice == '\x7f' or choice == '\x08':  # backspace
                            state.budget_input = state.budget_input[:-1]
                        elif choice == '\r' or choice == '\n':  # enter
                            if state.budget_input:
                                try:
                                    # Determine type from field definition
                                    ftype = "float"
                                    for _, _, ak, ft in BUDGET_FIELDS:
                                        if ak == state.budget_field_key:
                                            ftype = ft
                                            break
                                    val = float(state.budget_input) if ftype == "float" else int(state.budget_input)
                                    if patch_remote_config({state.budget_field_key: val}):
                                        state.last_config = get_remote_config()
                                        state.message = f"[green]{state.budget_field} → {val}[/green]"
                                        state.message_expiry = now + 2
                                    else:
                                        state.message = "[red]Failed to update (server unreachable)[/red]"
                                        state.message_expiry = now + 2
                                except ValueError:
                                    state.message = "[red]Invalid number[/red]"
                                    state.message_expiry = now + 2
                            state.menu = "main"
                            state.budget_field = None
                            state.budget_input = ""
                        elif choice == '\x1b':  # escape
                            state.menu = "budget"
                            state.budget_field = None
                            state.budget_input = ""

                    elif state.menu == "cost_provider":
                        if ch == '0' or ch == '\x1b':
                            state.menu = "main"
                        else:
                            providers = sorted(state.last_config.get("providers", {}).keys()) if state.last_config else ["deepseek", "groq", "ollama"]
                            if ch.isdigit() and 1 <= int(ch) <= len(providers):
                                cp = providers[int(ch) - 1]
                                state.cost_provider = cp
                                state.message = f"[dim]Fetching models for {cp}...[/dim]"
                                state.message_expiry = now + 10
                                state.needs_update = True

                                def fetch_cost_models(pname):
                                    res = check_remote_provider(pname)
                                    state.available_models[pname] = res.get("models", [])
                                    state.cost_menu_options = res.get("models", [])
                                    state.menu = "cost_model"
                                    state.message = ""
                                    state.needs_update = True

                                Thread(target=fetch_cost_models, args=(cp,), daemon=True).start()
                            else:
                                state.menu = "main"

                    elif state.menu == "cost_model":
                        if ch == '0' or ch == '\x1b':
                            state.menu = "cost_provider"
                        elif ch.isdigit() and 1 <= int(ch) <= len(state.cost_menu_options):
                            state.cost_model = state.cost_menu_options[int(ch) - 1]
                            state.menu = "cost_field"
                        else:
                            state.menu = "main"

                    elif state.menu == "cost_field":
                        if ch == '0' or ch == '\x1b':
                            state.menu = "cost_model"
                        else:
                            f_map = {"1": "input_cost_per_million", "2": "output_cost_per_million", "3": "cached_input_cost_per_million"}
                            if ch in f_map:
                                state.cost_field = f_map[ch]
                                state.budget_input = ""
                                state.menu = "cost_input"
                            else:
                                state.menu = "cost_model"

                    elif state.menu == "cost_input":
                        if choice in '0123456789.':
                            state.budget_input += choice
                        elif choice == '\x7f' or choice == '\x08':  # backspace
                            state.budget_input = state.budget_input[:-1]
                        elif choice == '\x1b':  # escape
                            state.menu = "cost_field"
                        elif choice == '\r' or choice == '\n':  # enter
                            if state.budget_input:
                                try:
                                    val = float(state.budget_input)
                                    pricing_data = {state.cost_field: val}
                                    if state.cost_model:
                                        pricing_data["model"] = state.cost_model
                                    updates = {
                                        "provider_pricing": {
                                            state.cost_provider: pricing_data
                                        }
                                    }
                                    if patch_remote_config(updates):
                                        state.last_config = get_remote_config()
                                        state.message = f"[green]Updated {state.cost_provider} {state.cost_field} → {val}[/green]"
                                        state.message_expiry = now + 3
                                    else:
                                        state.message = "[red]Failed to update (server unreachable)[/red]"
                                        state.message_expiry = now + 2
                                except ValueError:
                                    state.message = "[red]Invalid number[/red]"
                                    state.message_expiry = now + 2
                            state.menu = "main"
                            state.cost_provider = None
                            state.cost_field = None
                            state.budget_input = ""
                        elif choice == '\x1b' or choice == '0':  # escape
                            state.menu = "cost_field"
                            state.budget_input = ""

                except queue.Empty:
                    pass

                if state.needs_update:
                    update_layout(layout)
                    state.needs_update = False

                time.sleep(0.05)

    except KeyboardInterrupt:
        return 130
    finally:
        stop_event.set()

    return 0




def cmd_run(args: argparse.Namespace) -> int:
    command = command_after_separator(args.cmd)
    if not command:
        print("run: expected a command after --", file=sys.stderr)
        return 2
    settings = settings_from_args(args, ephemeral_port=True)

    proxy_env = {**os.environ, **settings.as_env(include_secrets=True)}
    proxy_proc = subprocess.Popen(
        [sys.executable, "-m", "subagent_router.cli", "start"],
        env=proxy_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_for_health(settings)
        child_env = {
            key: value
            for key, value in os.environ.items()
            if key not in _CHILD_SECRET_KEYS
        }
        child_env.update(settings.as_env())
        result = subprocess.run(
            inject_codex_overrides(command, settings),
            env=child_env,
        )
        return result.returncode
    finally:
        proxy_proc.terminate()
        try:
            proxy_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proxy_proc.kill()
            proxy_proc.wait()


def start_background(args: argparse.Namespace, settings: Settings) -> int:
    pid_path = pid_file(settings)
    existing_pid = read_pid(settings)
    if existing_pid is not None:
        if process_running(existing_pid):
            print(f"Subagent Router is already running (pid {existing_pid}).", file=sys.stderr)
            return 1
        pid_path.unlink(missing_ok=True)

    settings.state_dir.mkdir(parents=True, exist_ok=True)
    log_path = server_log_file(settings)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proxy_env = {**os.environ, **settings.as_env(include_secrets=True)}
    command = [sys.executable, "-m", "subagent_router.cli", "start"]

    with log_path.open("ab", buffering=0) as log_handle:
        proc = subprocess.Popen(
            command,
            env=proxy_env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pid_path.write_text(f"{proc.pid}\n", encoding="utf-8")
    try:
        wait_for_health(settings)
        ensure_background_child_running(proc, pid_path, log_path)
    except Exception as exc:
        if proc.poll() is None:
            proc.terminate()
        pid_path.unlink(missing_ok=True)
        print(f"Subagent Router failed to start: {exc}", file=sys.stderr)
        print(f"Log: {log_path}", file=sys.stderr)
        return 1

    print(f"Started Subagent Router (pid {proc.pid}) at http://{settings.host}:{settings.port}/v1")
    print(f"Log: {log_path}")
    if args.attach_logs:
        return print_log(log_path, lines=40, follow=True)
    return 0


def stop_background(settings: Settings, *, timeout: float, force: bool, quiet: bool) -> int:
    pid_path = pid_file(settings)
    pid = read_pid(settings)
    if pid is None:
        if not quiet:
            print("Subagent Router is not running.")
        return 0
    if not process_running(pid):
        pid_path.unlink(missing_ok=True)
        if not quiet:
            print(f"Subagent Router is not running (removed stale pid {pid}).")
        return 0

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_path.unlink(missing_ok=True)
        if not quiet:
            print(f"Subagent Router is not running (removed stale pid {pid}).")
        return 0
    except PermissionError:
        print(f"Permission denied while stopping Subagent Router (pid {pid}).", file=sys.stderr)
        return 1

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_running(pid):
            pid_path.unlink(missing_ok=True)
            if not quiet:
                print(f"Stopped Subagent Router (pid {pid}).")
            return 0
        time.sleep(0.1)

    if force:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pid_path.unlink(missing_ok=True)
            if not quiet:
                print(f"Subagent Router is not running (removed stale pid {pid}).")
            return 0
        except PermissionError:
            print(f"Permission denied while killing Subagent Router (pid {pid}).", file=sys.stderr)
            return 1
        pid_path.unlink(missing_ok=True)
        if not quiet:
            print(f"Killed Subagent Router (pid {pid}).")
        return 0

    print(f"Subagent Router did not stop within {timeout:g}s (pid {pid}).", file=sys.stderr)
    print("Use --force to kill it.", file=sys.stderr)
    return 1


def cmd_paths(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    delegation_profile = installed_delegation_profile()
    if args.json:
        print(json.dumps({
            **settings.sanitized_paths(),
            "provider_id": PROVIDER_ID,
            "provider_name": PROVIDER_NAME,
            "delegation_profile": delegation_profile,
            "deepseek_base_url": settings.deepseek_base_url,
            "deepseek_model": settings.deepseek_model or "default",
            "mock_deepseek": settings.mock_deepseek,
            "allow_apply_patch": settings.allow_apply_patch,
            "trace_enabled": settings.trace_enabled,
            "provider": settings.provider,
            "fallback_providers": settings.fallback_providers,
            "providers": sanitized_provider_summary(settings),
            "audit_log_file": str(settings.audit_log_file),
            "usage_file": str(settings.usage_file),
            "usage_jsonl_file": str(settings.usage_jsonl_file),
        }, indent=2))
    else:
        for name, path in settings.sanitized_paths().items():
            print(f"{name}: {path}")
        print(f"provider_id: {PROVIDER_ID}")
        print(f"provider_name: {PROVIDER_NAME}")
        print(f"delegation_profile: {delegation_profile}")
        print(f"host: {settings.host}")
        print(f"port: {settings.port}")
        print(f"deepseek_base_url: {settings.deepseek_base_url}")
        print(f"deepseek_model: {settings.deepseek_model or 'default'}")
        print(f"mock_deepseek: {settings.mock_deepseek}")
        print(f"allow_apply_patch: {settings.allow_apply_patch}")
        print(f"trace_enabled: {settings.trace_enabled}")
        print(f"provider: {settings.provider}")
        print(f"fallback_providers: {','.join(settings.fallback_providers) or 'none'}")
        print(f"audit_log_file: {settings.audit_log_file}")
        print(f"usage_file: {settings.usage_file}")
        print(f"usage_jsonl_file: {settings.usage_jsonl_file}")
        for name, provider in sanitized_provider_summary(settings).items():
            print(f"provider.{name}: type={provider['type']} kind={provider['kind']} enabled={provider['enabled']}")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    package_version = _pkg_version_str()
    if args.json:
        print(json.dumps({"version": package_version}))
    else:
        print(package_version)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    issues: list[str] = []

    selected_provider = settings.providers.get(settings.provider)
    if selected_provider is None:
        issues.append(f"provider {settings.provider!r} is not configured")
    elif not selected_provider.enabled:
        issues.append(f"provider {settings.provider!r} is disabled")
    elif selected_provider.kind != "local" and not selected_provider.api_key and not settings.mock_deepseek:
        if selected_provider.provider_type == "deepseek":
            issues.append("DEEPSEEK_API_KEY is not set (use --mock or export DEEPSEEK_API_KEY)")
        else:
            issues.append(f"provider {settings.provider!r} requires an API key")
    for provider_name in settings.fallback_providers:
        provider = settings.providers.get(provider_name)
        if provider is None:
            issues.append(f"fallback provider {provider_name!r} is not configured")
        elif not provider.enabled:
            issues.append(f"fallback provider {provider_name!r} is disabled")

    if settings.state_dir == Path():
        issues.append("state_dir could not be resolved")

    for name, path in settings.sanitized_paths().items():
        if not can_create_dir(Path(path).parent if name.endswith("_file") else Path(path)):
            issues.append(f"{name}: cannot create {path}")

    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print("Configuration looks good.")
    if args.json:
        print(json.dumps({
            "healthy": True,
            "issues": issues,
            "provider": settings.provider,
            "delegation_profile": installed_delegation_profile(),
        }))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).expanduser().resolve()
    proxy_url = args.proxy_url.rstrip("/")
    try:
        profile = normalize_profile(args.profile)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    codex_home.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest(codex_home)

    if args.mode == "default":
        _init_default(codex_home, proxy_url, manifest, profile=profile, force=args.force)
    elif args.mode == "opt-in":
        if args.profile is not None:
            print("warning: --profile is ignored with --mode opt-in; profile only affects --mode default", file=sys.stderr)
        _init_opt_in(codex_home, proxy_url, manifest, force=args.force)
    elif args.mode == "provider-only":
        if args.profile is not None:
            print("warning: --profile is ignored with --mode provider-only; profile only affects --mode default", file=sys.stderr)
        _init_provider_only(codex_home, proxy_url, manifest, force=args.force)

    _save_manifest(codex_home, manifest)
    return 0


def _switch_delegation_profile(
    codex_home: Path,
    proxy_url: str,
    profile: str,
    *,
    force: bool,
) -> tuple[bool, str, str | None]:
    """Run the default init flow for a profile switch.

    Returns (switched, normalized_profile, error). switched is false with no
    error when init preserved a user-customized instruction file.
    """
    try:
        normalized = normalize_profile(profile)
        codex_home.mkdir(parents=True, exist_ok=True)
        manifest = _load_manifest(codex_home)
        _init_default(codex_home, proxy_url.rstrip("/"), manifest, profile=normalized, force=force)
        _save_manifest(codex_home, manifest)
    except Exception as exc:
        return False, str(profile), str(exc)

    installed = installed_delegation_profile(codex_home)
    return installed == normalized, normalized, None


def _init_default(
    codex_home: Path,
    proxy_url: str,
    manifest: dict,
    *,
    profile: str,
    force: bool,
) -> None:
    if profile == "manual":
        _init_manual(codex_home, proxy_url, manifest, force=force)
        return

    instructions = subagent_router_instructions_for_profile(profile)
    # Write the delegation instructions file.
    system_file = codex_home / "SUBAGENT_ROUTER_INSTRUCTIONS.md"
    action = _managed_file_action(
        system_file,
        instructions,
        manifest.get("files", {}).get("SUBAGENT_ROUTER_INSTRUCTIONS.md"),
        force=force,
        legacy_hashes=LEGACY_MANAGED_HASHES.get("SUBAGENT_ROUTER_INSTRUCTIONS.md", ()),
    )
    if action in ("write", "adopt"):
        if action == "write":
            system_file.write_text(instructions, encoding="utf-8")
        manifest["delegation_profile"] = profile
        _update_manifest_file_entry(manifest, "SUBAGENT_ROUTER_INSTRUCTIONS.md", instructions)

    # Reference the instructions file from AGENTS.md.
    agents_path = codex_home / "AGENTS.md"
    instruction_path = system_file.resolve()
    ref_line = f"Follow instructions in {instruction_path}"
    legacy_ref_line = f"{instruction_path}"
    if agents_path.exists():
        existing = agents_path.read_text(encoding="utf-8")
        lines = existing.splitlines()
        if lines and lines[0] == ref_line:
            pass  # already present
        elif lines and lines[0] == legacy_ref_line:
            trailing_newline = "\n" if existing.endswith("\n") else ""
            agents_path.write_text(
                f"{ref_line}\n" + "\n".join(lines[1:]) + trailing_newline,
                encoding="utf-8",
            )
        else:
            agents_path.write_text(f"{ref_line}\n{existing}", encoding="utf-8")
    else:
        agents_path.write_text(f"{ref_line}\n", encoding="utf-8")

    # Write agent role files.
    _write_router_agent_files(codex_home, manifest, force=force)

    # Write provider config block to config.toml.
    _write_provider_config(codex_home, proxy_url, force=force)


def _init_manual(
    codex_home: Path,
    proxy_url: str,
    manifest: dict,
    *,
    force: bool,
) -> None:
    system_file = codex_home / "SUBAGENT_ROUTER_INSTRUCTIONS.md"
    prev_entry = manifest.get("files", {}).get("SUBAGENT_ROUTER_INSTRUCTIONS.md")
    action = _managed_file_action(
        system_file,
        "",
        prev_entry,
        force=force,
        legacy_hashes=LEGACY_MANAGED_HASHES.get("SUBAGENT_ROUTER_INSTRUCTIONS.md", ()),
    )
    if system_file.exists() and action == "write":
        system_file.unlink()
        manifest.get("files", {}).pop("SUBAGENT_ROUTER_INSTRUCTIONS.md", None)

    agents_path = codex_home / "AGENTS.md"
    if agents_path.exists():
        instruction_path = system_file.resolve()
        ref_line = f"Follow instructions in {instruction_path}"
        legacy_ref_line = f"{instruction_path}"
        existing = agents_path.read_text(encoding="utf-8")
        lines = existing.splitlines()
        if lines and lines[0] in (ref_line, legacy_ref_line):
            trailing_newline = "\n" if existing.endswith("\n") and len(lines) > 1 else ""
            agents_path.write_text("\n".join(lines[1:]) + trailing_newline, encoding="utf-8")

    manifest["delegation_profile"] = "manual"
    _write_router_agent_files(codex_home, manifest, force=force)
    _write_provider_config(codex_home, proxy_url, force=force)


def _init_opt_in(
    codex_home: Path,
    proxy_url: str,
    manifest: dict,
    *,
    force: bool,
) -> None:
    skill_dir = codex_home / "skills" / "deepseek"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    action = _managed_file_action(
        skill_path,
        DEEPSEEK_SKILL,
        manifest.get("files", {}).get("skills/deepseek/SKILL.md"),
        force=force,
    )
    if action == "write":
        skill_path.write_text(DEEPSEEK_SKILL, encoding="utf-8")
    _update_manifest_file_entry(manifest, "skills/deepseek/SKILL.md", DEEPSEEK_SKILL)

    sc_dir = codex_home / "slash_commands"
    sc_dir.mkdir(parents=True, exist_ok=True)
    sc_path = sc_dir / "deepseek.md"
    action = _managed_file_action(
        sc_path,
        DEEPSEEK_SLASH_COMMAND,
        manifest.get("files", {}).get("slash_commands/deepseek.md"),
        force=force,
    )
    if action == "write":
        sc_path.write_text(DEEPSEEK_SLASH_COMMAND, encoding="utf-8")
    _update_manifest_file_entry(manifest, "slash_commands/deepseek.md", DEEPSEEK_SLASH_COMMAND)

    _write_router_agent_files(codex_home, manifest, force=force)
    _write_provider_config(codex_home, proxy_url, force=force)


def _init_provider_only(
    codex_home: Path,
    proxy_url: str,
    manifest: dict,
    *,
    force: bool,
) -> None:
    _write_router_agent_files(codex_home, manifest, force=force)
    _write_provider_config(codex_home, proxy_url, force=force)


def _write_router_agent_files(codex_home: Path, manifest: dict, *, force: bool) -> None:
    _write_agent_file(codex_home, manifest, "agents/subagent-router-explorer.toml", EXPLORER_AGENT, force=force)
    _write_agent_file(codex_home, manifest, "agents/subagent-router-worker.toml", WORKER_AGENT, force=force)
    _write_agent_file(codex_home, manifest, "agents/subagent-router-reviewer.toml", REVIEWER_AGENT, force=force)


def _write_agent_file(
    codex_home: Path,
    manifest: dict,
    relative_path: str,
    content: str,
    *,
    force: bool,
) -> None:
    path = codex_home / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    prev_entry = manifest.get("files", {}).get(relative_path)
    legacy_hashes = LEGACY_MANAGED_HASHES.get(relative_path, ())
    action = _managed_file_action(
        path, content, prev_entry, force=force, legacy_hashes=legacy_hashes
    )
    if action == "write":
        path.write_text(content, encoding="utf-8")
    _update_manifest_file_entry(manifest, relative_path, content)


def _write_provider_config(
    codex_home: Path,
    proxy_url: str,
    *,
    force: bool,
) -> None:
    config_path = codex_home / "config.toml"
    block = _provider_config_block(proxy_url)

    if not config_path.exists():
        config_path.write_text(block + "\n", encoding="utf-8")
        return

    existing = config_path.read_text(encoding="utf-8")
    if CONFIG_MARKER_BEGIN in existing and CONFIG_MARKER_END in existing:
        config_path.write_text(
            replace_marked_block(existing, block) + "\n", encoding="utf-8"
        )
    elif force:
        config_path.write_text(existing.rstrip("\n") + "\n\n" + block + "\n", encoding="utf-8")
    else:
        # Append new block if markers not found.
        config_path.write_text(existing.rstrip("\n") + "\n\n" + block + "\n", encoding="utf-8")


def cmd_install_service(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    exec_path = shutil.which("subagent-router")
    if not exec_path:
        exec_path = f"{Path(sys.executable).parent}/subagent-router"
    unit = systemd_unit(exec_path, settings)
    unit_path = Path.home() / ".config" / "systemd" / "user" / f"{args.name}.service"
    if unit_path.exists() and not args.force:
        print(f"{unit_path} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(unit, encoding="utf-8")
    print(f"Wrote {unit_path}")
    print("Enable with: systemctl --user enable --now", args.name)
    return 0


# ---------------------------------------------------------------------------
# Proxy runner
# ---------------------------------------------------------------------------


def proxy_main(settings: Settings) -> None:
    from .app import app
    import uvicorn

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


# ---------------------------------------------------------------------------
# Provider config helpers
# ---------------------------------------------------------------------------


def _provider_config_block(proxy_url: str) -> str:
    return f'''{CONFIG_MARKER_BEGIN}
[model_providers.{PROVIDER_ID}]
name = "{PROVIDER_NAME}"
base_url = "{proxy_url}"
wire_api = "responses"
requires_openai_auth = false
{CONFIG_MARKER_END}'''


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _pkg_version_str() -> str:
    root = Path(__file__).resolve().parents[2]
    package_json = root / "package.json"
    if package_json.exists():
        data = json.loads(package_json.read_text(encoding="utf-8"))
        package_version = data.get("version")
        if isinstance(package_version, str):
            return package_version

    try:
        return _pkg_version("subagent-router")
    except PackageNotFoundError:
        pyproject = root / "pyproject.toml"
        if pyproject.exists():
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            project_version = data.get("project", {}).get("version")
            if isinstance(project_version, str):
                return project_version

        return "0+unknown"


def _load_manifest(codex_home: Path) -> dict:
    manifest_path = codex_home / MANIFEST_FILENAME
    if manifest_path.exists():
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    return {"package_version": "", "files": {}}


def _save_manifest(codex_home: Path, manifest: dict) -> None:
    manifest["package_version"] = _pkg_version_str()
    manifest_path = codex_home / MANIFEST_FILENAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def installed_delegation_profile(codex_home: Path | None = None) -> str:
    root = codex_home
    if root is None:
        root = Path(os.getenv("CODEX_HOME", "~/.codex")).expanduser()
    manifest_path = root / MANIFEST_FILENAME
    if not manifest_path.exists():
        return DEFAULT_PROFILE
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        profile = manifest.get("delegation_profile")
        if isinstance(profile, str):
            return normalize_profile(profile)
    except (OSError, ValueError, json.JSONDecodeError):
        return DEFAULT_PROFILE
    return DEFAULT_PROFILE


def _update_manifest_file_entry(manifest: dict, relative_path: str, content: str) -> None:
    """Update or add a file entry in the managed manifest."""
    if "files" not in manifest:
        manifest["files"] = {}
    manifest["files"][relative_path] = {
        "content_hash": _hash_content(content),
    }


def _managed_file_action(
    path: Path,
    new_content: str,
    prev_entry: dict | None,
    *,
    force: bool,
    legacy_hashes: Sequence[str] = (),
) -> str:
    """Return write/adopt/preserve for a managed activation file."""
    if force:
        return "write"
    if not path.exists():
        return "write"

    new_hash = _hash_content(new_content)
    on_disk_hash = _hash_content(path.read_text(encoding="utf-8"))

    if on_disk_hash == new_hash:
        return "adopt" if prev_entry is None else "preserve"

    prev_hash = prev_entry.get("content_hash", "") if prev_entry else ""
    if on_disk_hash == prev_hash:
        return "write"
    if prev_entry is None and on_disk_hash in legacy_hashes:
        return "write"

    return "preserve"


def replace_marked_block(text: str, block: str) -> str:
    return replace_marked_block_with_markers(
        text,
        block,
        begin=CONFIG_MARKER_BEGIN,
        end=CONFIG_MARKER_END,
    )


def replace_marked_block_with_markers(text: str, block: str, *, begin: str, end: str) -> str:
    before, rest = text.split(begin, 1)
    _, after = rest.split(end, 1)
    return f"{before}{block}{after}"


def inject_codex_overrides(command: Sequence[str], settings: Settings) -> list[str]:
    cmd = list(command)
    overrides = codex_override_args(settings)
    if Path(cmd[0]).name == "codex":
        return [cmd[0], *overrides, *cmd[1:]]
    return [*cmd, *overrides]


def codex_override_args(settings: Settings) -> list[str]:
    provider = (
        "{ "
        f'name = "{PROVIDER_NAME}", '
        f'base_url = "http://{settings.host}:{settings.port}/v1", '
        'wire_api = "responses", '
        "requires_openai_auth = false "
        "}"
    )
    return ["-c", f"model_providers.{PROVIDER_ID}={provider}"]


def command_after_separator(items: Sequence[str]) -> list[str]:
    values = list(items)
    if values and values[0] == "--":
        return values[1:]
    return values


def wait_for_health(settings: Settings, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://{settings.host}:{settings.port}/health"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # pragma: no cover - exercised in integration smoke.
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"proxy did not become healthy at {url}: {last_error}")


def ensure_background_child_running(proc: subprocess.Popen, pid_path: Path, log_path: Path) -> None:
    time.sleep(0.2)
    return_code = proc.poll()
    if return_code is None:
        return
    pid_path.unlink(missing_ok=True)
    raise RuntimeError(f"proxy process exited early with status {return_code}; see {log_path}")


def pid_file(settings: Settings) -> Path:
    return settings.state_dir / "subagent-router.pid"


def server_log_file(settings: Settings) -> Path:
    return settings.state_dir / "logs" / "server.log"


def read_pid(settings: Settings) -> int | None:
    path = pid_file(settings)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        path.unlink(missing_ok=True)
        return None


def process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def print_log(path: Path, *, lines: int, follow: bool) -> int:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        if lines > 0:
            for line in deque(handle, maxlen=lines):
                print(line, end="")
        else:
            handle.seek(0, os.SEEK_END)

        if not follow:
            return 0

        try:
            while True:
                line = handle.readline()
                if line:
                    print(line, end="")
                else:
                    time.sleep(0.2)
        except KeyboardInterrupt:
            return 130


def free_loopback_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def can_create_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        return False


def sanitized_provider_summary(settings: Settings) -> dict[str, dict[str, object]]:
    return {
        name: {
            "type": provider.provider_type,
            "kind": provider.kind,
            "enabled": provider.enabled,
            "base_url_configured": bool(provider.base_url),
            "model": provider.model,
            "api_key_configured": bool(provider.api_key),
            "capabilities": {
                "context_window": provider.capabilities.context_window,
                "tool_support": provider.capabilities.supports_tools,
                "streaming_support": provider.capabilities.supports_streaming,
                "cost_hint": provider.capabilities.cost_hint,
            },
        }
        for name, provider in settings.providers.items()
    }


def systemd_unit(exec_start: str, settings: Settings) -> str:
    env_lines = "\n".join(
        f'Environment="{key}={value}"'
        for key, value in settings.as_env().items()
    )
    return f"""[Unit]
Description=Subagent Router

[Service]
Type=simple
{env_lines}
ExecStart={exec_start}
Restart=on-failure

[Install]
WantedBy=default.target
"""



def cmd_validate_artifacts(args: argparse.Namespace) -> int:
    """Validate release artifacts for packaging consistency."""
    issues: list[str] = []
    warnings: list[str] = []
    checks: dict[str, bool | str] = {}

    # Version consistency
    root = Path(__file__).resolve().parents[2]
    source_tree = (root / ".git").exists() or (root / "tests").exists()
    pkg_json_path = root / "package.json"
    pyproject_path = root / "pyproject.toml"
    pkg_version: str | None = None
    py_version: str | None = None

    if pkg_json_path.exists():
        try:
            pkg = json.loads(pkg_json_path.read_text(encoding="utf-8"))
            pkg_version = pkg.get("version")
            checks["package_json_exists"] = True
        except (OSError, json.JSONDecodeError):
            issues.append("package.json exists but is not valid JSON")
            checks["package_json_exists"] = False
    else:
        issues.append("package.json not found")
        checks["package_json_exists"] = False

    if pyproject_path.exists():
        try:
            py_data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
            py_version = (py_data.get("project") or {}).get("version")
            checks["pyproject_toml_exists"] = True
        except (OSError, tomllib.TOMLDecodeError):
            issues.append("pyproject.toml exists but is not valid TOML")
            checks["pyproject_toml_exists"] = False
    else:
        if source_tree:
            issues.append("pyproject.toml not found")
        else:
            warnings.append(
                "pyproject.toml not found; this is expected for installed npm artifacts. "
                "Run validate-artifacts from the source tree for release validation."
            )
        checks["pyproject_toml_exists"] = False

    if pkg_version and py_version:
        if pkg_version == py_version:
            checks["version_consistency"] = True
        else:
            issues.append(f"version mismatch: package.json={pkg_version!r}, pyproject.toml={py_version!r}")
            checks["version_consistency"] = False
    elif pkg_version or py_version:
        checks["version_consistency"] = pkg_version or py_version
    else:
        checks["version_consistency"] = False

    # Check key source files exist
    src_dir = Path(__file__).resolve().parent
    app_py = src_dir / "app.py"
    settings_py = src_dir / "settings.py"
    checks["app_py_exists"] = app_py.exists()
    checks["settings_py_exists"] = settings_py.exists()
    if not app_py.exists():
        issues.append(f"app.py not found at {app_py}")
    if not settings_py.exists():
        issues.append(f"settings.py not found at {settings_py}")

    # Check CLI is importable
    try:
        from subagent_router import cli as _test_cli
        checks["cli_importable"] = True
    except Exception as exc:
        checks["cli_importable"] = False
        issues.append(f"cli module cannot be imported: {exc}")

    # Check npm packaging structure
    bin_dir = root / "bin"
    npm_bin = bin_dir / "subagent-router.js"
    checks["npm_bin_exists"] = npm_bin.exists()
    if not npm_bin.exists():
        issues.append(f"npm bin script not found at {npm_bin}")

    # Check docs directory
    docs_dir = root / "docs"
    required_docs = ["usage.md", "compatibility.md", "test_matrix.md", "ROADMAP.md", "troubleshooting.md"]
    for doc in required_docs:
        key = f"doc_{doc.replace('.', '_')}_exists"
        checks[key] = (docs_dir / doc).exists()

    # Check README
    checks["readme_exists"] = (root / "README.md").exists()
    checks["changelog_exists"] = (root / "CHANGELOG.md").exists()

    if args.json:
        print(json.dumps({
            "healthy": len(issues) == 0,
            "issues": issues,
            "warnings": warnings,
            "checks": {k: v for k, v in checks.items()},
            "package_version": pkg_version,
            "pyproject_version": py_version,
        }, indent=2, sort_keys=True))
    else:
        print(f"subagent-router validate-artifacts")
        print(f"  package.json version: {pkg_version or '(missing)'}")
        print(f"  pyproject.toml version: {py_version or '(missing)'}")
        version_ok = checks.get("version_consistency")
        print(f"  version consistency: {'OK' if version_ok is True else version_ok if isinstance(version_ok, str) else 'MISMATCH'}")
        print(f"  app.py: {'OK' if checks.get('app_py_exists') else 'MISSING'}")
        print(f"  settings.py: {'OK' if checks.get('settings_py_exists') else 'MISSING'}")
        print(f"  npm bin: {'OK' if checks.get('npm_bin_exists') else 'MISSING'}")
        print(f"  CLI importable: {'OK' if checks.get('cli_importable') else 'FAIL'}")
        print(f"  docs: {sum(1 for k, v in checks.items() if k.startswith('doc_') and v)}/{len(required_docs)} present")
        print(f"  README: {'OK' if checks.get('readme_exists') else 'MISSING'}")
        print(f"  CHANGELOG: {'OK' if checks.get('changelog_exists') else 'MISSING'}")
        if warnings:
            print(f"  warnings ({len(warnings)}):")
            for warning in warnings:
                print(f"    - {warning}")
        if issues:
            print(f"  issues ({len(issues)}):")
            for issue in issues:
                print(f"    - {issue}")
            return 1
        print("  All checks passed.")
    return 0

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
