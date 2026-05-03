from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Sequence
from urllib.request import urlopen

from .activation import (
    DEEPSEEK_SKILL,
    DEEPSEEK_SLASH_COMMAND,
    REVIEWER_AGENT,
    SUBAGENT_ROUTER_SYSTEM_INSTRUCTIONS,
    WORKER_AGENT,
)
from .settings import Settings


PROVIDER_ID = "subagent_router"
PROVIDER_NAME = "Subagent Router"
CONFIG_MARKER_BEGIN = "# >>> subagent-router >>>"
CONFIG_MARKER_END = "# <<< subagent-router <<<"
MANIFEST_FILENAME = ".subagent-router-manifest.json"
LEGACY_MANAGED_HASHES: dict[str, tuple[str, ...]] = {}
_CHILD_SECRET_KEYS: frozenset[str] = frozenset({
    "DEEPSEEK_API_KEY",
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
    logs.set_defaults(handler=cmd_logs)

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
    init.set_defaults(handler=cmd_init)

    service = subcommands.add_parser("install-service", help="write a systemd user service")
    add_common_settings_args(service)
    service.add_argument("--name", default="subagent-router")
    service.add_argument("--force", action="store_true")
    service.set_defaults(handler=cmd_install_service)

    return parser


def add_common_settings_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--log-dir", default=None)
    parser.add_argument("--activity-file", default=None)
    parser.add_argument("--session-mirror-file", default=None)
    parser.add_argument("--provider-error-log-dir", default=None)
    parser.add_argument("--deepseek-base-url", default=None)
    parser.add_argument("--model", default=None, help="override upstream DeepSeek model")
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
        "DEEPSEEK_BASE_URL": args.deepseek_base_url,
        "DEEPSEEK_MODEL": args.model,
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
    if pid is None:
        print("Subagent Router is not running.")
        return 1
    if not process_running(pid):
        pid_file(settings).unlink(missing_ok=True)
        print(f"Subagent Router is not running (removed stale pid {pid}).")
        return 1
    print(f"Subagent Router is running (pid {pid}) at http://{settings.host}:{settings.port}/v1")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    path = server_log_file(settings)
    if not path.exists():
        print(f"No server log found at {path}", file=sys.stderr)
        return 1
    return print_log(path, lines=args.lines, follow=args.follow)


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
    if args.json:
        print(json.dumps({
            **settings.sanitized_paths(),
            "provider_id": PROVIDER_ID,
            "provider_name": PROVIDER_NAME,
            "deepseek_base_url": settings.deepseek_base_url,
            "deepseek_model": settings.deepseek_model or "default",
            "mock_deepseek": settings.mock_deepseek,
            "allow_apply_patch": settings.allow_apply_patch,
            "trace_enabled": settings.trace_enabled,
        }, indent=2))
    else:
        for name, path in settings.sanitized_paths().items():
            print(f"{name}: {path}")
        print(f"provider_id: {PROVIDER_ID}")
        print(f"provider_name: {PROVIDER_NAME}")
        print(f"host: {settings.host}")
        print(f"port: {settings.port}")
        print(f"deepseek_base_url: {settings.deepseek_base_url}")
        print(f"deepseek_model: {settings.deepseek_model or 'default'}")
        print(f"mock_deepseek: {settings.mock_deepseek}")
        print(f"allow_apply_patch: {settings.allow_apply_patch}")
        print(f"trace_enabled: {settings.trace_enabled}")
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

    if not settings.mock_deepseek and not settings.deepseek_api_key:
        issues.append("DEEPSEEK_API_KEY is not set (use --mock or export DEEPSEEK_API_KEY)")

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
        print(json.dumps({"healthy": True, "issues": issues}))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).expanduser().resolve()
    proxy_url = args.proxy_url.rstrip("/")

    codex_home.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest(codex_home)

    if args.mode == "default":
        _init_default(codex_home, proxy_url, manifest, force=args.force)
    elif args.mode == "opt-in":
        _init_opt_in(codex_home, proxy_url, manifest, force=args.force)
    elif args.mode == "provider-only":
        _init_provider_only(codex_home, proxy_url, manifest, force=args.force)

    _save_manifest(codex_home, manifest)
    return 0


def _init_default(
    codex_home: Path,
    proxy_url: str,
    manifest: dict,
    *,
    force: bool,
) -> None:
    # Write the delegation instructions file.
    system_file = codex_home / "SUBAGENT_ROUTER_INSTRUCTIONS.md"
    action = _managed_file_action(
        system_file,
        SUBAGENT_ROUTER_SYSTEM_INSTRUCTIONS,
        manifest.get("files", {}).get("SUBAGENT_ROUTER_INSTRUCTIONS.md"),
        force=force,
    )
    if action == "write":
        system_file.write_text(SUBAGENT_ROUTER_SYSTEM_INSTRUCTIONS, encoding="utf-8")
    _update_manifest_file_entry(manifest, "SUBAGENT_ROUTER_INSTRUCTIONS.md", SUBAGENT_ROUTER_SYSTEM_INSTRUCTIONS)

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
    _write_agent_file(codex_home, manifest, "agents/subagent-router-worker.toml", WORKER_AGENT, force=force)
    _write_agent_file(codex_home, manifest, "agents/subagent-router-reviewer.toml", REVIEWER_AGENT, force=force)

    # Write provider config block to config.toml.
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

    _write_agent_file(codex_home, manifest, "agents/subagent-router-worker.toml", WORKER_AGENT, force=force)
    _write_agent_file(codex_home, manifest, "agents/subagent-router-reviewer.toml", REVIEWER_AGENT, force=force)
    _write_provider_config(codex_home, proxy_url, force=force)


def _init_provider_only(
    codex_home: Path,
    proxy_url: str,
    manifest: dict,
    *,
    force: bool,
) -> None:
    _write_agent_file(codex_home, manifest, "agents/subagent-router-worker.toml", WORKER_AGENT, force=force)
    _write_agent_file(codex_home, manifest, "agents/subagent-router-reviewer.toml", REVIEWER_AGENT, force=force)
    _write_provider_config(codex_home, proxy_url, force=force)


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
    try:
        return _pkg_version("subagent-router")
    except PackageNotFoundError:
        root = Path(__file__).resolve().parents[2]
        package_json = root / "package.json"
        if package_json.exists():
            data = json.loads(package_json.read_text(encoding="utf-8"))
            package_version = data.get("version")
            if isinstance(package_version, str):
                return package_version

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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
