import datetime
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock

from subagent_router import cli
from subagent_router.activation import normalize_profile
from subagent_router.settings import Settings


class CliTests(unittest.TestCase):
    def test_short_cli_paths_prints_resolved_paths_json(self):
        with tempfile.TemporaryDirectory() as state_dir:
            stream = io.StringIO()

            with redirect_stdout(stream):
                result = cli.main(["paths", "--state-dir", state_dir, "--json"])

        self.assertEqual(result, 0)
        paths = json.loads(stream.getvalue())
        self.assertEqual(paths["state_dir"], str(Path(state_dir).resolve()))
        self.assertEqual(paths["activity_file"], str(Path(state_dir).resolve() / "logs" / "activity.json"))
        self.assertIn("providers", paths)
        self.assertIn("delegation_profile", paths)
        self.assertEqual(paths["delegation_profile"], "cost-optimization")

    def test_doctor_succeeds_in_mock_mode_without_api_key(self):
        with tempfile.TemporaryDirectory() as state_dir:
            result = cli.main(["doctor", "--state-dir", state_dir, "--mock"])

        self.assertEqual(result, 0)

    def test_doctor_succeeds_for_enabled_local_provider_without_api_key(self):
        with tempfile.TemporaryDirectory() as state_dir:
            result = cli.main([
                "doctor",
                "--state-dir",
                state_dir,
                "--provider",
                "ollama",
                "--ollama-enabled",
            ])

        self.assertEqual(result, 0)

    def test_doctor_fails_without_mock_or_api_key(self):
        with tempfile.TemporaryDirectory() as state_dir:
            with patch.dict(os.environ, {}, clear=True):
                result = cli.main(["doctor", "--state-dir", state_dir])

        self.assertEqual(result, 1)

    def test_init_default_installs_router_instructions_and_references_them_from_agents(self):
        with tempfile.TemporaryDirectory() as codex_home:
            result = cli.main(["init", "--codex-home", codex_home, "--proxy-url", "http://127.0.0.1:9999/v1"])

            root = Path(codex_home)
            self.assertEqual(result, 0)
            instructions = (root / "SUBAGENT_ROUTER_INSTRUCTIONS.md").read_text()
            self.assertIn("standing user authorization", instructions)
            self.assertIn("Active profile: cost-optimization", instructions)
            self.assertIn("minimize parent Codex/GPT-5.5 token usage", instructions)
            self.assertEqual(
                (root / "AGENTS.md").read_text().splitlines()[0],
                f"Follow instructions in {(root / 'SUBAGENT_ROUTER_INSTRUCTIONS.md').resolve()}",
            )
            self.assertFalse((root / "skills" / "deepseek" / "SKILL.md").exists())
            self.assertFalse((root / "slash_commands" / "deepseek.md").exists())
            self.assertIn("subagent_router_explorer", (root / "agents" / "subagent-router-explorer.toml").read_text())
            self.assertIn("subagent_router_worker", (root / "agents" / "subagent-router-worker.toml").read_text())
            self.assertIn("subagent_router_reviewer", (root / "agents" / "subagent-router-reviewer.toml").read_text())
            config = (root / "config.toml").read_text()
            self.assertIn("[model_providers.subagent_router]", config)
            self.assertIn('base_url = "http://127.0.0.1:9999/v1"', config)

    def test_profile_normalization_accepts_canonical_values_and_aliases(self):
        self.assertEqual(normalize_profile(None), "cost-optimization")
        self.assertEqual(normalize_profile("cost"), "cost-optimization")
        self.assertEqual(normalize_profile("cost-optimized"), "cost-optimization")
        self.assertEqual(normalize_profile("budget"), "cost-optimization")
        self.assertEqual(normalize_profile("budget-optimized"), "cost-optimization")
        self.assertEqual(normalize_profile("token-saving"), "cost-optimization")
        self.assertEqual(normalize_profile("token-optimized"), "cost-optimization")
        self.assertEqual(normalize_profile("deep"), "deep-delegation")
        self.assertEqual(normalize_profile("deep-delegate"), "deep-delegation")
        self.assertEqual(normalize_profile("deep-delegation"), "deep-delegation")
        self.assertEqual(normalize_profile("aggressive"), "deep-delegation")
        self.assertEqual(normalize_profile("aggressive-delegation"), "deep-delegation")
        self.assertEqual(normalize_profile("quality"), "orchestrator")
        self.assertEqual(normalize_profile("conservative"), "orchestrator")
        self.assertEqual(normalize_profile("codex-control"), "orchestrator")
        self.assertEqual(normalize_profile("orchestrator"), "orchestrator")
        self.assertEqual(normalize_profile("manual"), "manual")
        self.assertEqual(normalize_profile("opt-in"), "manual")
        self.assertEqual(normalize_profile("provider-only"), "manual")

    def test_init_explicit_cost_optimization_profile_installs_cost_instructions(self):
        with tempfile.TemporaryDirectory() as codex_home:
            result = cli.main(["init", "--codex-home", codex_home, "--profile", "cost-optimization"])

            root = Path(codex_home)
            self.assertEqual(result, 0)
            instructions = (root / "SUBAGENT_ROUTER_INSTRUCTIONS.md").read_text(encoding="utf-8")
            self.assertIn("Active profile: cost-optimization", instructions)
            self.assertIn("Retry and continuation caps", instructions)
            self.assertIn("These limits apply regardless of", instructions)
            self.assertIn("Parent hard-stop triggers", instructions)
            self.assertIn("tests fail or hang after delegated implementation", instructions)
            self.assertIn("patch source or test logic", instructions)
            manifest = json.loads((root / ".subagent-router-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["delegation_profile"], "cost-optimization")

    def test_init_explicit_deep_delegation_profile_installs_deep_instructions(self):
        with tempfile.TemporaryDirectory() as codex_home:
            result = cli.main(["init", "--codex-home", codex_home, "--profile", "deep-delegation"])

            root = Path(codex_home)
            self.assertEqual(result, 0)
            instructions = (root / "SUBAGENT_ROUTER_INSTRUCTIONS.md").read_text(encoding="utf-8")
            self.assertIn("Active profile: deep-delegation", instructions)
            self.assertIn("maximize offload", instructions)
            manifest = json.loads((root / ".subagent-router-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["delegation_profile"], "deep-delegation")

    def test_init_explicit_orchestrator_profile_installs_orchestrator_instructions(self):
        with tempfile.TemporaryDirectory() as codex_home:
            result = cli.main(["init", "--codex-home", codex_home, "--profile", "orchestrator"])

            root = Path(codex_home)
            self.assertEqual(result, 0)
            instructions = (root / "SUBAGENT_ROUTER_INSTRUCTIONS.md").read_text(encoding="utf-8")
            self.assertIn("Active profile: orchestrator", instructions)
            self.assertIn("primary orchestrator", instructions)
            manifest = json.loads((root / ".subagent-router-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["delegation_profile"], "orchestrator")

    def test_init_explicit_manual_profile_skips_global_instructions(self):
        with tempfile.TemporaryDirectory() as codex_home:
            result = cli.main(["init", "--codex-home", codex_home, "--profile", "manual"])

            root = Path(codex_home)
            self.assertEqual(result, 0)
            self.assertFalse((root / "SUBAGENT_ROUTER_INSTRUCTIONS.md").exists())
            self.assertFalse((root / "AGENTS.md").exists())
            self.assertTrue((root / "agents" / "subagent-router-explorer.toml").exists())
            self.assertTrue((root / "agents" / "subagent-router-worker.toml").exists())
            manifest = json.loads((root / ".subagent-router-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["delegation_profile"], "manual")
            self.assertNotIn("SUBAGENT_ROUTER_INSTRUCTIONS.md", manifest.get("files", {}))

    def test_init_manual_profile_removes_managed_global_instructions(self):
        with tempfile.TemporaryDirectory() as codex_home:
            root = Path(codex_home)
            self.assertEqual(cli.main(["init", "--codex-home", codex_home, "--profile", "cost-optimization"]), 0)
            self.assertTrue((root / "SUBAGENT_ROUTER_INSTRUCTIONS.md").exists())

            result = cli.main(["init", "--codex-home", codex_home, "--profile", "manual"])

            self.assertEqual(result, 0)
            self.assertFalse((root / "SUBAGENT_ROUTER_INSTRUCTIONS.md").exists())
            self.assertFalse((root / "AGENTS.md").read_text(encoding="utf-8").strip())
            manifest = json.loads((root / ".subagent-router-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["delegation_profile"], "manual")

    def test_installed_agent_templates_include_required_output_contracts(self):
        with tempfile.TemporaryDirectory() as codex_home:
            root = Path(codex_home)
            result = cli.main(["init", "--codex-home", codex_home])

            self.assertEqual(result, 0)
            explorer = (root / "agents" / "subagent-router-explorer.toml").read_text(encoding="utf-8")
            worker = (root / "agents" / "subagent-router-worker.toml").read_text(encoding="utf-8")
            reviewer = (root / "agents" / "subagent-router-reviewer.toml").read_text(encoding="utf-8")
            self.assertIn('model = "subagent-router-explorer"', explorer)
            self.assertIn("RECOMMENDED_NEXT_AGENT: explorer|worker|reviewer|none", explorer)
            self.assertIn('model = "subagent-router-worker"', worker)
            self.assertIn("FILES_CHANGED:", worker)
            self.assertIn("NEEDS_PARENT_ACTION: yes|no", worker)
            self.assertIn('model = "subagent-router-reviewer"', reviewer)
            self.assertIn("NO_FINDINGS: true|false", reviewer)

    def test_init_invalid_profile_fails_clearly(self):
        with tempfile.TemporaryDirectory() as codex_home:
            stream = io.StringIO()
            with redirect_stderr(stream):
                result = cli.main(["init", "--codex-home", codex_home, "--profile", "reckless"])

        self.assertEqual(result, 2)
        self.assertIn("invalid delegation profile", stream.getvalue())
        self.assertIn("cost-optimization", stream.getvalue())
        self.assertIn("deep-delegation", stream.getvalue())
        self.assertIn("orchestrator", stream.getvalue())

    def test_init_default_preserves_existing_agents_content_below_router_path(self):
        with tempfile.TemporaryDirectory() as codex_home:
            root = Path(codex_home)
            agents_path = root / "AGENTS.md"
            agents_path.write_text("/home/example/OTHER.md\n\n# Existing\nKeep me.\n", encoding="utf-8")

            result = cli.main(["init", "--codex-home", codex_home])

            self.assertEqual(result, 0)
            lines = agents_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], f"Follow instructions in {(root / 'SUBAGENT_ROUTER_INSTRUCTIONS.md').resolve()}")
            self.assertIn("/home/example/OTHER.md", lines)
            self.assertIn("Keep me.", lines)

    def test_init_default_replaces_legacy_bare_router_path(self):
        with tempfile.TemporaryDirectory() as codex_home:
            root = Path(codex_home)
            agents_path = root / "AGENTS.md"
            legacy_path = (root / "SUBAGENT_ROUTER_INSTRUCTIONS.md").resolve()
            agents_path.write_text(f"{legacy_path}\n\n# Existing\nKeep me.\n", encoding="utf-8")

            result = cli.main(["init", "--codex-home", codex_home])

            self.assertEqual(result, 0)
            lines = agents_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], f"Follow instructions in {legacy_path}")
            self.assertNotIn(str(legacy_path), lines[1:])
            self.assertIn("Keep me.", lines)

    def test_init_opt_in_installs_skill_and_slash_without_global_instructions(self):
        with tempfile.TemporaryDirectory() as codex_home:
            result = cli.main(["init", "--mode", "opt-in", "--codex-home", codex_home, "--profile", "orchestrator"])

            root = Path(codex_home)
            self.assertEqual(result, 0)
            self.assertFalse((root / "AGENTS.md").exists())
            self.assertFalse((root / "SUBAGENT_ROUTER_INSTRUCTIONS.md").exists())
            self.assertIn("Use this skill only", (root / "skills" / "deepseek" / "SKILL.md").read_text())
            self.assertIn("$deepseek {args}", (root / "slash_commands" / "deepseek.md").read_text())
            self.assertTrue((root / "agents" / "subagent-router-explorer.toml").exists())

    def test_init_provider_only_skips_global_and_opt_in_activation(self):
        with tempfile.TemporaryDirectory() as codex_home:
            result = cli.main(["init", "--mode", "provider-only", "--codex-home", codex_home, "--profile", "cost"])

            root = Path(codex_home)
            self.assertEqual(result, 0)
            self.assertFalse((root / "AGENTS.md").exists())
            self.assertFalse((root / "SUBAGENT_ROUTER_INSTRUCTIONS.md").exists())
            self.assertFalse((root / "skills" / "deepseek" / "SKILL.md").exists())
            self.assertFalse((root / "slash_commands" / "deepseek.md").exists())
            self.assertTrue((root / "agents" / "subagent-router-explorer.toml").exists())
            self.assertTrue((root / "agents" / "subagent-router-worker.toml").exists())

    def test_init_opt_in_with_profile_warns_on_stderr(self):
        with tempfile.TemporaryDirectory() as codex_home:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = cli.main(["init", "--mode", "opt-in", "--codex-home", codex_home, "--profile", "orchestrator"])
            self.assertEqual(result, 0)
            self.assertIn("--profile is ignored with --mode opt-in", stderr.getvalue())

    def test_init_provider_only_with_profile_warns_on_stderr(self):
        with tempfile.TemporaryDirectory() as codex_home:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = cli.main(["init", "--mode", "provider-only", "--codex-home", codex_home, "--profile", "cost"])
            self.assertEqual(result, 0)
            self.assertIn("--profile is ignored with --mode provider-only", stderr.getvalue())

    def test_init_does_not_overwrite_existing_activation_without_force(self):
        with tempfile.TemporaryDirectory() as codex_home:
            agent_path = Path(codex_home) / "agents" / "subagent-router-worker.toml"
            agent_path.parent.mkdir(parents=True)
            agent_path.write_text("custom", encoding="utf-8")

            cli.main(["init", "--codex-home", codex_home])

            self.assertEqual(agent_path.read_text(encoding="utf-8"), "custom")

    def test_codex_overrides_are_inserted_after_codex_executable(self):
        settings = Settings.from_env({"CODEX_PROXY_HOST": "127.0.0.1", "CODEX_PROXY_PORT": "4567"})

        command = cli.inject_codex_overrides(["codex", "exec", "hello"], settings)

        self.assertEqual(command[0], "codex")
        self.assertEqual(command[1], "-c")
        self.assertIn("model_providers.subagent_router", command[2])
        self.assertEqual(command[-2:], ["exec", "hello"])

    def test_run_requires_command_after_separator(self):
        result = cli.main(["run", "--"])

        self.assertEqual(result, 2)

    def test_top_level_help_lists_background_lifecycle_examples(self):
        help_text = cli.build_parser().format_help()

        self.assertIn("subagent-router start --background", help_text)
        self.assertIn("subagent-router start --background --attach-logs", help_text)
        self.assertIn("subagent-router logs --follow", help_text)
        self.assertIn("subagent-router restart", help_text)
        self.assertIn("subagent-router stop", help_text)
        self.assertIn("subagent-router version", help_text)

    def test_init_help_lists_profile_option(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["init", "--help"])

        self.assertEqual(ctx.exception.code, 0)
        help_text = stream.getvalue()
        self.assertIn("--profile", help_text)
        self.assertIn("cost-optimization", help_text)
        self.assertIn("deep-delegation", help_text)
        self.assertIn("orchestrator", help_text)
        self.assertIn("manual", help_text)

    def test_version_command_prints_package_version(self):
        stream = io.StringIO()

        with redirect_stdout(stream):
            result = cli.main(["version"])

        self.assertEqual(result, 0)
        self.assertEqual(stream.getvalue(), f"{cli._pkg_version_str()}\n")

    def test_version_command_prints_json(self):
        stream = io.StringIO()

        with redirect_stdout(stream):
            result = cli.main(["version", "--json"])

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stream.getvalue()), {"version": cli._pkg_version_str()})

    @patch("subagent_router.cli.wait_for_health")
    @patch("subagent_router.cli.subprocess.Popen")
    def test_start_background_writes_pid_and_log_path(self, mock_popen, mock_wait):
        proc = Mock()
        proc.pid = 12345
        proc.poll.return_value = None
        mock_popen.return_value = proc

        with tempfile.TemporaryDirectory() as state_dir:
            result = cli.main(["start", "--state-dir", state_dir, "--mock", "--background"])
            root = Path(state_dir)

            self.assertEqual(result, 0)
            self.assertEqual((root / "subagent-router.pid").read_text(encoding="utf-8"), "12345\n")
            self.assertTrue((root / "logs" / "server.log").exists())

        command = mock_popen.call_args.args[0]
        kwargs = mock_popen.call_args.kwargs
        self.assertEqual(command, [os.sys.executable, "-m", "subagent_router.cli", "start"])
        self.assertEqual(kwargs["env"]["DEEPSEEK_PROXY_MOCK"], "1")
        self.assertEqual(kwargs["stdin"], cli.subprocess.DEVNULL)
        self.assertTrue(kwargs["start_new_session"])

    @patch("subagent_router.cli.wait_for_health", side_effect=RuntimeError("not healthy"))
    @patch("subagent_router.cli.subprocess.Popen")
    def test_start_background_cleans_pid_when_health_check_fails(self, mock_popen, mock_wait):
        proc = Mock()
        proc.pid = 12345
        proc.poll.return_value = None
        mock_popen.return_value = proc

        with tempfile.TemporaryDirectory() as state_dir:
            result = cli.main(["start", "--state-dir", state_dir, "--mock", "--background"])

            self.assertEqual(result, 1)
            self.assertFalse((Path(state_dir) / "subagent-router.pid").exists())
            proc.terminate.assert_called_once()

    @patch("subagent_router.cli.wait_for_health")
    @patch("subagent_router.cli.subprocess.Popen")
    def test_start_background_fails_when_child_exits_after_health_check(self, mock_popen, mock_wait):
        proc = Mock()
        proc.pid = 12345
        proc.poll.return_value = 1
        mock_popen.return_value = proc

        with tempfile.TemporaryDirectory() as state_dir:
            result = cli.main(["start", "--state-dir", state_dir, "--mock", "--background"])

            self.assertEqual(result, 1)
            self.assertFalse((Path(state_dir) / "subagent-router.pid").exists())

    @patch("subagent_router.cli.os.kill")
    def test_stop_removes_pid_after_process_exits(self, mock_kill):
        with tempfile.TemporaryDirectory() as state_dir:
            pid_path = Path(state_dir) / "subagent-router.pid"
            pid_path.write_text("12345\n", encoding="utf-8")

            def fake_kill(pid, sig):
                if sig == 0 and fake_kill.checks > 0:
                    raise ProcessLookupError()
                fake_kill.checks += 1

            fake_kill.checks = 0
            mock_kill.side_effect = fake_kill

            result = cli.main(["stop", "--state-dir", state_dir, "--timeout", "0.2"])

            self.assertEqual(result, 0)
            self.assertFalse(pid_path.exists())
            mock_kill.assert_any_call(12345, cli.signal.SIGTERM)

    @patch("subagent_router.cli.os.kill")
    def test_stop_removes_pid_when_sigterm_races_with_exit(self, mock_kill):
        def fake_kill(pid, sig):
            if sig == 0:
                return None
            raise ProcessLookupError()

        mock_kill.side_effect = fake_kill

        with tempfile.TemporaryDirectory() as state_dir:
            pid_path = Path(state_dir) / "subagent-router.pid"
            pid_path.write_text("12345\n", encoding="utf-8")

            result = cli.main(["stop", "--state-dir", state_dir])

            self.assertEqual(result, 0)
            self.assertFalse(pid_path.exists())
            mock_kill.assert_any_call(12345, cli.signal.SIGTERM)

    @patch("subagent_router.cli.os.kill")
    def test_stop_reports_permission_denied_for_sigterm(self, mock_kill):
        def fake_kill(pid, sig):
            if sig == 0:
                return None
            raise PermissionError()

        mock_kill.side_effect = fake_kill

        with tempfile.TemporaryDirectory() as state_dir:
            (Path(state_dir) / "subagent-router.pid").write_text("12345\n", encoding="utf-8")
            stream = io.StringIO()

            with redirect_stderr(stream):
                result = cli.main(["stop", "--state-dir", state_dir])

        self.assertEqual(result, 1)
        self.assertIn("Permission denied", stream.getvalue())

    @patch("subagent_router.cli.process_running", return_value=True)
    def test_start_background_refuses_existing_running_pid(self, mock_running):
        with tempfile.TemporaryDirectory() as state_dir:
            (Path(state_dir) / "subagent-router.pid").write_text("12345\n", encoding="utf-8")

            result = cli.main(["start", "--state-dir", state_dir, "--mock", "--background"])

        self.assertEqual(result, 1)

    @patch("subagent_router.cli.process_running", return_value=True)
    def test_status_reports_running_process(self, mock_running):
        with tempfile.TemporaryDirectory() as state_dir:
            (Path(state_dir) / "subagent-router.pid").write_text("12345\n", encoding="utf-8")
            stream = io.StringIO()

            with redirect_stdout(stream):
                result = cli.main(["status", "--state-dir", state_dir, "--host", "127.0.0.1", "--port", "9999"])

        self.assertEqual(result, 0)
        self.assertIn("pid 12345", stream.getvalue())
        self.assertIn("http://127.0.0.1:9999/v1", stream.getvalue())
        self.assertIn("Delegation Profile", stream.getvalue())
        self.assertIn("cost-optimization", stream.getvalue())

    @patch("subagent_router.cli.process_running", return_value=False)
    def test_status_removes_stale_pid(self, mock_running):
        with tempfile.TemporaryDirectory() as state_dir:
            pid_path = Path(state_dir) / "subagent-router.pid"
            pid_path.write_text("12345\n", encoding="utf-8")
            stream = io.StringIO()

            with redirect_stdout(stream):
                result = cli.main(["status", "--state-dir", state_dir])

            self.assertEqual(result, 1)
            self.assertFalse(pid_path.exists())
            self.assertIn("removed stale pid 12345", stream.getvalue())
            self.assertIn("Delegation Profile", stream.getvalue())

    def test_status_reports_missing_pid(self):
        with tempfile.TemporaryDirectory() as state_dir:
            stream = io.StringIO()

            with redirect_stdout(stream):
                result = cli.main(["status", "--state-dir", state_dir])

        self.assertEqual(result, 1)
        self.assertIn("not running", stream.getvalue())
        self.assertIn("Delegation Profile", stream.getvalue())

    def test_logs_reports_missing_log_file(self):
        with tempfile.TemporaryDirectory() as state_dir:
            stream = io.StringIO()

            with redirect_stderr(stream):
                result = cli.main(["logs", "--state-dir", state_dir])

        self.assertEqual(result, 1)
        self.assertIn("No server log found", stream.getvalue())

    def test_usage_prints_summary_json(self):
        with tempfile.TemporaryDirectory() as state_dir:
            usage_file = Path(state_dir) / "logs" / "usage.json"
            usage_file.parent.mkdir()
            usage_file.write_text(
                json.dumps(
                    {
                        "request_count": 2,
                        "total_tokens": 12,
                        "total_cost_usd": 0.01,
                        "requests_by_provider": {"deepseek": 2},
                        "requests_by_model": {"deepseek-chat": 2},
                    }
                ),
                encoding="utf-8",
            )
            stream = io.StringIO()

            with redirect_stdout(stream):
                result = cli.main(["usage", "--state-dir", state_dir, "--json"])

            self.assertEqual(result, 0)
            self.assertEqual(json.loads(stream.getvalue())["request_count"], 2)

    def test_debug_bundle_includes_diagnostic_files(self):
        with tempfile.TemporaryDirectory() as state_dir:
            root = Path(state_dir)
            (root / "logs").mkdir()
            (root / "logs" / "activity.json").write_text("{}", encoding="utf-8")
            output = root / "bundle.tar.gz"
            stream = io.StringIO()

            with redirect_stdout(stream):
                result = cli.main(["debug-bundle", "--state-dir", state_dir, "--output", str(output)])

            self.assertEqual(result, 0)
            self.assertTrue(output.exists())

    def test_stdio_returns_mock_response_json(self):
        payload = json.dumps({"model": "deepseek-chat", "stream": False, "input": "hello", "tools": []})
        stdin = io.StringIO(payload)
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as state_dir:
            with patch("sys.stdin", stdin), redirect_stdout(stdout):
                result = cli.main(["stdio", "--state-dir", state_dir, "--mock"])

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue())["output"][0]["type"], "message")

    def test_handoff_once_writes_response_file(self):
        with tempfile.TemporaryDirectory() as state_dir, tempfile.TemporaryDirectory() as task_dir:
            task_path = Path(task_dir) / "task.json"
            task_path.write_text(
                json.dumps({"model": "deepseek-chat", "stream": False, "input": "hello", "tools": []}),
                encoding="utf-8",
            )

            result = cli.main([
                "handoff",
                "--state-dir",
                state_dir,
                "--mock",
                "--input-dir",
                task_dir,
                "--once",
            ])

            response = json.loads((Path(task_dir) / "task.response.json").read_text(encoding="utf-8"))
        self.assertEqual(result, 0)
        self.assertEqual(response["output"][0]["type"], "message")

    def test_tui_prints_status(self):
        with tempfile.TemporaryDirectory() as state_dir:
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = cli.main(["tui", "--state-dir", state_dir, "--mock"])

        self.assertEqual(result, 0)
        self.assertIn("Subagent Router", stream.getvalue())
        self.assertIn("Profile", stream.getvalue())
        self.assertIn("[D] Profile", stream.getvalue())

    def test_tui_uses_codex_home_for_installed_profile(self):
        with tempfile.TemporaryDirectory() as state_dir, tempfile.TemporaryDirectory() as codex_home:
            self.assertEqual(cli.main(["init", "--codex-home", codex_home, "--profile", "orchestrator"]), 0)
            stream = io.StringIO()

            with redirect_stdout(stream):
                result = cli.main(["tui", "--state-dir", state_dir, "--codex-home", codex_home, "--mock"])

        self.assertEqual(result, 0)
        self.assertIn("orchestrator", stream.getvalue())

    def test_switch_delegation_profile_runs_default_init(self):
        with tempfile.TemporaryDirectory() as codex_home:
            root = Path(codex_home)
            self.assertEqual(cli.main(["init", "--codex-home", codex_home, "--profile", "cost-optimization"]), 0)

            switched, profile, error = cli._switch_delegation_profile(
                root,
                "http://127.0.0.1:8787/v1",
                "orchestrator",
                force=False,
            )

            self.assertTrue(switched)
            self.assertEqual(profile, "orchestrator")
            self.assertIsNone(error)
            self.assertIn("Active profile: orchestrator", (root / "SUBAGENT_ROUTER_INSTRUCTIONS.md").read_text(encoding="utf-8"))
            manifest = json.loads((root / ".subagent-router-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["delegation_profile"], "orchestrator")

    def test_tui_profile_switch_force_overwrites_custom_instructions(self):
        with tempfile.TemporaryDirectory() as codex_home:
            root = Path(codex_home)
            self.assertEqual(cli.main(["init", "--codex-home", codex_home, "--profile", "cost-optimization"]), 0)
            instruction_path = root / "SUBAGENT_ROUTER_INSTRUCTIONS.md"
            instruction_path.write_text("# Custom profile instructions\n", encoding="utf-8")

            switched, profile, error = cli._switch_delegation_profile(
                root,
                "http://127.0.0.1:8787/v1",
                "orchestrator",
                force=True,
            )

            self.assertTrue(switched)
            self.assertEqual(profile, "orchestrator")
            self.assertIsNone(error)
            text = instruction_path.read_text(encoding="utf-8")
            self.assertIn("Active profile: orchestrator", text)
            self.assertNotIn("# Custom profile instructions", text)

    def test_switch_delegation_profile_preserves_custom_instructions_without_force(self):
        with tempfile.TemporaryDirectory() as codex_home:
            root = Path(codex_home)
            self.assertEqual(cli.main(["init", "--codex-home", codex_home, "--profile", "cost-optimization"]), 0)
            instruction_path = root / "SUBAGENT_ROUTER_INSTRUCTIONS.md"
            custom_text = "# Custom profile instructions\n"
            instruction_path.write_text(custom_text, encoding="utf-8")

            switched, profile, error = cli._switch_delegation_profile(
                root,
                "http://127.0.0.1:8787/v1",
                "orchestrator",
                force=False,
            )

            self.assertFalse(switched)
            self.assertEqual(profile, "orchestrator")
            self.assertIsNone(error)
            text = instruction_path.read_text(encoding="utf-8")
            self.assertEqual(text, custom_text)
            manifest = json.loads((root / ".subagent-router-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["delegation_profile"], "cost-optimization")

    def test_tui_with_watch_refreshes_until_interrupt(self):
        with tempfile.TemporaryDirectory() as state_dir:
            stream = io.StringIO()
            with redirect_stdout(stream):
                with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    with patch("time.sleep", side_effect=KeyboardInterrupt):
                        result = cli.main(["tui", "--state-dir", state_dir, "--watch"])

        self.assertEqual(result, 130)

    def test_tui_shows_activity_and_usage(self):
        with tempfile.TemporaryDirectory() as state_dir:
            logs_dir = Path(state_dir) / "logs"
            logs_dir.mkdir(parents=True)
            usage = {
                "total_cost_usd": 0.025,
                "daily_usage": {
                    datetime.date.today().isoformat(): {
                        "total_cost_usd": 0.025,
                        "total_tokens": 1200,
                        "input_tokens": 900,
                        "cached_input_tokens": 300,
                        "output_tokens": 300,
                    }
                }
            }
            (logs_dir / "usage.json").write_text(json.dumps(usage), encoding="utf-8")
            audit_path = logs_dir / "audit.jsonl"
            audit_path.write_text(
                json.dumps({
                    "timestamp": time.time(),
                    "status": "success",
                    "provider": "deepseek",
                    "estimated_cost_usd": 0.005
                }) + "\n",
                encoding="utf-8"
            )

            stream = io.StringIO()
            with redirect_stdout(stream):
                result = cli.main(["tui", "--state-dir", state_dir])

        self.assertEqual(result, 0)
        output = stream.getvalue()
        self.assertIn("Configuration", output)
        self.assertIn("Budgets", output)
        self.assertIn("Daily $", output)
        self.assertIn("900 / 300 / 300", output)
        self.assertIn("Recent Requests", output)
        self.assertIn("deepseek", output)

    def test_tui_shows_error_details_for_failed_requests(self):
        with tempfile.TemporaryDirectory() as state_dir:
            logs_dir = Path(state_dir) / "logs"
            logs_dir.mkdir(parents=True)
            (logs_dir / "usage.json").write_text(
                json.dumps({"daily_usage": {datetime.date.today().isoformat(): {}}}),
                encoding="utf-8",
            )
            audit_path = logs_dir / "audit.jsonl"
            audit_path.write_text(
                json.dumps(
                    {
                        "timestamp": time.time(),
                        "status": "error",
                        "provider": "deepseek",
                        "model": "deepseek-v4-flash",
                        "status_code": 502,
                        "message": "provider transport error: read timeout",
                        "estimated_cost_usd": 0.0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            stream = io.StringIO()
            with redirect_stdout(stream):
                result = cli.main(["tui", "--state-dir", state_dir])

        self.assertEqual(result, 0)
        output = stream.getvalue()
        self.assertIn("ERR", output)
        self.assertIn("read timeout", output)

    def test_tui_extracts_provider_error_message_from_repr_payload(self):
        with tempfile.TemporaryDirectory() as state_dir:
            logs_dir = Path(state_dir) / "logs"
            logs_dir.mkdir(parents=True)
            (logs_dir / "usage.json").write_text(
                json.dumps({"daily_usage": {datetime.date.today().isoformat(): {}}}),
                encoding="utf-8",
            )
            audit_path = logs_dir / "audit.jsonl"
            audit_path.write_text(
                json.dumps(
                    {
                        "timestamp": time.time(),
                        "status": "error",
                        "provider": "deepseek",
                        "model": "deepseek-v4-flash",
                        "message": "provider rejected request: {'error': {'message': 'insufficient balance', 'type': 'billing'}}",
                        "estimated_cost_usd": 0.0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            stream = io.StringIO()
            with redirect_stdout(stream):
                result = cli.main(["tui", "--state-dir", state_dir])

        self.assertEqual(result, 0)
        output = stream.getvalue()
        self.assertIn("ERR", output)
        self.assertIn("insufficient balance", output)
        self.assertNotIn("{'error'", output)

    def test_tui_does_not_mark_recovery_audit_records_as_errors(self):
        with tempfile.TemporaryDirectory() as state_dir:
            logs_dir = Path(state_dir) / "logs"
            logs_dir.mkdir(parents=True)
            (logs_dir / "usage.json").write_text(
                json.dumps({"daily_usage": {datetime.date.today().isoformat(): {}}}),
                encoding="utf-8",
            )
            audit_path = logs_dir / "audit.jsonl"
            audit_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": time.time(),
                                "status": "incomplete-output-retry",
                                "provider": "deepseek",
                                "model": "deepseek-v4-flash",
                                "reason": "provider returned empty output: no assistant message or tool call",
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": time.time(),
                                "status": "synthetic-continuation",
                                "provider": "deepseek",
                                "model": "deepseek-v4-flash",
                                "reason": "provider returned empty output: no assistant message or tool call",
                                "tool": "exec_command",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            stream = io.StringIO()
            with redirect_stdout(stream):
                result = cli.main(["tui", "--state-dir", state_dir])

        self.assertEqual(result, 0)
        output = stream.getvalue()
        self.assertIn("RETRY", output)
        self.assertIn("CONT", output)
        self.assertNotIn("ERR", output)

    def test_validate_artifacts_passes(self):
        result = cli.main(["validate-artifacts"])
        self.assertEqual(result, 0)

    def test_validate_artifacts_json_output(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            result = cli.main(["validate-artifacts", "--json"])
        data = json.loads(stream.getvalue())
        self.assertEqual(result, 0)
        self.assertTrue(data["healthy"])
        self.assertTrue(data["checks"]["version_consistency"])
        self.assertEqual(data["package_version"], data["pyproject_version"])

    def test_validate_artifacts_allows_missing_pyproject_outside_source_tree(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            src_dir = root / "src" / "subagent_router"
            docs_dir = root / "docs"
            bin_dir = root / "bin"
            src_dir.mkdir(parents=True)
            docs_dir.mkdir()
            bin_dir.mkdir()
            for path in (
                src_dir / "app.py",
                src_dir / "settings.py",
                bin_dir / "subagent-router.js",
                root / "README.md",
                docs_dir / "usage.md",
                docs_dir / "compatibility.md",
                docs_dir / "test_matrix.md",
                docs_dir / "ROADMAP.md",
                docs_dir / "troubleshooting.md",
            ):
                path.write_text("", encoding="utf-8")
            (root / "package.json").write_text('{"version":"0.1.9"}', encoding="utf-8")

            old_file = cli.__file__
            cli.__file__ = str(src_dir / "cli.py")
            try:
                stream = io.StringIO()
                with redirect_stdout(stream):
                    result = cli.main(["validate-artifacts", "--json"])
            finally:
                cli.__file__ = old_file

        data = json.loads(stream.getvalue())
        self.assertEqual(result, 0)
        self.assertTrue(data["healthy"])
        self.assertFalse(data["checks"]["pyproject_toml_exists"])
        self.assertEqual(data["checks"]["version_consistency"], "0.1.9")
        self.assertEqual(data["package_version"], "0.1.9")
        self.assertIsNone(data["pyproject_version"])
        self.assertTrue(data["warnings"])

    def test_logs_prints_trailing_lines(self):
        with tempfile.TemporaryDirectory() as state_dir:
            log_path = Path(state_dir) / "logs" / "server.log"
            log_path.parent.mkdir(parents=True)
            log_path.write_text("one\ntwo\nthree\n", encoding="utf-8")
            stream = io.StringIO()

            with redirect_stdout(stream):
                result = cli.main(["logs", "--state-dir", state_dir, "--lines", "2"])

        self.assertEqual(result, 0)
        self.assertEqual(stream.getvalue(), "two\nthree\n")

    @patch("uvicorn.run")
    def test_cmd_start_restores_process_environment(self, mock_uvicorn_run):
        def assert_runtime_env(*args, **kwargs):
            self.assertEqual(os.environ.get("DEEPSEEK_PROXY_MOCK"), "1")
            self.assertEqual(os.environ.get("SUBAGENT_ROUTER_PORT"), "9999")

        mock_uvicorn_run.side_effect = assert_runtime_env

        with patch.dict(os.environ, {"KEEP_ME": "yes"}, clear=True):
            result = cli.main(["start", "--mock", "--port", "9999"])
            restored_env = dict(os.environ)

        self.assertEqual(result, 0)
        self.assertEqual(restored_env, {"KEEP_ME": "yes"})

    def test_init_first_install_creates_manifest(self):
        with tempfile.TemporaryDirectory() as codex_home:
            result = cli.main(["init", "--codex-home", codex_home])
            root = Path(codex_home)
            manifest_path = root / ".subagent-router-manifest.json"
            self.assertEqual(result, 0)
            self.assertTrue(manifest_path.exists())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["package_version"], cli._pkg_version_str())
            self.assertEqual(manifest["delegation_profile"], "cost-optimization")
            self.assertIn("agents/subagent-router-explorer.toml", manifest["files"])
            self.assertIn("agents/subagent-router-worker.toml", manifest["files"])
            self.assertIn("agents/subagent-router-reviewer.toml", manifest["files"])
            self.assertIn("SUBAGENT_ROUTER_INSTRUCTIONS.md", manifest["files"])
            for key, entry in manifest["files"].items():
                self.assertIn("content_hash", entry)
                self.assertTrue(len(entry["content_hash"]) > 0)

    def test_init_same_version_no_destructive_overwrite(self):
        with tempfile.TemporaryDirectory() as codex_home:
            root = Path(codex_home)
            result1 = cli.main(["init", "--codex-home", codex_home])
            self.assertEqual(result1, 0)

            worker_path = root / "agents" / "subagent-router-worker.toml"
            first_stat = worker_path.stat()
            manifest_first = json.loads((root / ".subagent-router-manifest.json").read_text(encoding="utf-8"))

            result2 = cli.main(["init", "--codex-home", codex_home])
            self.assertEqual(result2, 0)

            manifest_second = json.loads((root / ".subagent-router-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest_first, manifest_second)
            self.assertEqual(worker_path.stat().st_mtime, first_stat.st_mtime)

    def test_init_older_version_triggers_update(self):
        with tempfile.TemporaryDirectory() as codex_home:
            result = cli.main(["init", "--codex-home", codex_home])
            root = Path(codex_home)
            self.assertEqual(result, 0)

            worker_path = root / "agents" / "subagent-router-worker.toml"
            current_content = worker_path.read_text(encoding="utf-8")
            current_hash = cli._hash_content(current_content)

            old_content = "# This simulates content installed by an older package version\n"
            old_hash = cli._hash_content(old_content)
            worker_path.write_text(old_content, encoding="utf-8")

            manifest_path = root / ".subagent-router-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"]["agents/subagent-router-worker.toml"]["content_hash"] = old_hash
            manifest["package_version"] = "0.0.0"
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = cli.main(["init", "--codex-home", codex_home])
            self.assertEqual(result, 0)

            self.assertEqual(worker_path.read_text(encoding="utf-8"), current_content)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["files"]["agents/subagent-router-worker.toml"]["content_hash"], current_hash)

    def test_init_profile_switch_updates_managed_instruction_file(self):
        with tempfile.TemporaryDirectory() as codex_home:
            root = Path(codex_home)
            result = cli.main(["init", "--codex-home", codex_home, "--profile", "cost-optimization"])
            self.assertEqual(result, 0)
            instruction_path = root / "SUBAGENT_ROUTER_INSTRUCTIONS.md"
            self.assertIn("Active profile: cost-optimization", instruction_path.read_text(encoding="utf-8"))

            result = cli.main(["init", "--codex-home", codex_home, "--profile", "orchestrator"])
            self.assertEqual(result, 0)

            self.assertIn("Active profile: orchestrator", instruction_path.read_text(encoding="utf-8"))
            manifest = json.loads((root / ".subagent-router-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["delegation_profile"], "orchestrator")

    def test_init_profile_switch_preserves_custom_instruction_file_without_force(self):
        with tempfile.TemporaryDirectory() as codex_home:
            root = Path(codex_home)
            cli.main(["init", "--codex-home", codex_home, "--profile", "cost-optimization"])
            instruction_path = root / "SUBAGENT_ROUTER_INSTRUCTIONS.md"
            instruction_path.write_text("# Custom profile instructions\n", encoding="utf-8")

            result = cli.main(["init", "--codex-home", codex_home, "--profile", "orchestrator"])
            self.assertEqual(result, 0)

            self.assertEqual(instruction_path.read_text(encoding="utf-8"), "# Custom profile instructions\n")

            manifest = json.loads((root / ".subagent-router-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["delegation_profile"], "cost-optimization")

    def test_init_force_overwrites_custom_instruction_file_on_profile_switch(self):
        with tempfile.TemporaryDirectory() as codex_home:
            root = Path(codex_home)
            cli.main(["init", "--codex-home", codex_home, "--profile", "cost-optimization"])
            instruction_path = root / "SUBAGENT_ROUTER_INSTRUCTIONS.md"
            instruction_path.write_text("# Custom profile instructions\n", encoding="utf-8")

            result = cli.main(["init", "--codex-home", codex_home, "--profile", "orchestrator", "--force"])
            self.assertEqual(result, 0)

            text = instruction_path.read_text(encoding="utf-8")
            self.assertIn("Active profile: orchestrator", text)
            self.assertNotIn("# Custom profile instructions", text)

    def test_init_adopts_preexisting_cost_optimization_instructions(self):
        with tempfile.TemporaryDirectory() as codex_home:
            root = Path(codex_home)
            instruction_path = root / "SUBAGENT_ROUTER_INSTRUCTIONS.md"
            instructions = cli.subagent_router_instructions_for_profile("cost-optimization")
            instruction_path.parent.mkdir(parents=True, exist_ok=True)
            instruction_path.write_text(instructions, encoding="utf-8")

            manifest_path = root / ".subagent-router-manifest.json"
            self.assertFalse(manifest_path.exists())

            result = cli.main(["init", "--codex-home", codex_home, "--profile", "cost-optimization"])
            self.assertEqual(result, 0)

            self.assertTrue(manifest_path.exists())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["delegation_profile"], "cost-optimization")
            instructions_entry = manifest.get("files", {}).get("SUBAGENT_ROUTER_INSTRUCTIONS.md")
            self.assertIsNotNone(instructions_entry)
            self.assertIn("content_hash", instructions_entry)
            self.assertIsInstance(instructions_entry["content_hash"], str)
            self.assertTrue(len(instructions_entry["content_hash"]) > 0)

    def test_init_user_modification_preserved_without_force(self):
        with tempfile.TemporaryDirectory() as codex_home:
            result = cli.main(["init", "--codex-home", codex_home, "--proxy-url", "http://127.0.0.1:9999/v1"])
            root = Path(codex_home)
            self.assertEqual(result, 0)

            worker_path = root / "agents" / "subagent-router-worker.toml"
            modified = "# User customization\n"
            worker_path.write_text(modified, encoding="utf-8")

            result = cli.main(["init", "--codex-home", codex_home])
            self.assertEqual(result, 0)

            self.assertEqual(worker_path.read_text(encoding="utf-8"), modified)

    def test_init_legacy_managed_hash_updates_without_manifest(self):
        with tempfile.TemporaryDirectory() as codex_home:
            root = Path(codex_home)
            worker_path = root / "agents" / "subagent-router-worker.toml"
            worker_path.parent.mkdir(parents=True)
            legacy_content = "# old managed worker template\n"
            worker_path.write_text(legacy_content, encoding="utf-8")

            with patch.dict(
                cli.LEGACY_MANAGED_HASHES,
                {"agents/subagent-router-worker.toml": (cli._hash_content(legacy_content),)},
                clear=True,
            ):
                result = cli.main(["init", "--codex-home", codex_home])

            self.assertEqual(result, 0)
            self.assertIn("subagent_router_worker", worker_path.read_text(encoding="utf-8"))

    def test_init_legacy_instructions_adopted_without_manifest(self):
        with tempfile.TemporaryDirectory() as codex_home:
            root = Path(codex_home)
            instruction_path = root / "SUBAGENT_ROUTER_INSTRUCTIONS.md"
            legacy_content = "# legacy custom instructions\n"
            instruction_path.write_text(legacy_content, encoding="utf-8")

            with patch.dict(
                cli.LEGACY_MANAGED_HASHES,
                {"SUBAGENT_ROUTER_INSTRUCTIONS.md": (cli._hash_content(legacy_content),)},
                clear=True,
            ):
                result = cli.main(["init", "--codex-home", codex_home])

            self.assertEqual(result, 0)
            updated = instruction_path.read_text(encoding="utf-8")
            self.assertIn("Active profile: cost-optimization", updated)
            manifest = json.loads((root / ".subagent-router-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["delegation_profile"], "cost-optimization")

    def test_init_force_overwrites_user_modifications(self):
        with tempfile.TemporaryDirectory() as codex_home:
            result = cli.main(["init", "--codex-home", codex_home, "--proxy-url", "http://127.0.0.1:9999/v1"])
            root = Path(codex_home)
            self.assertEqual(result, 0)

            worker_path = root / "agents" / "subagent-router-worker.toml"
            original_content = worker_path.read_text(encoding="utf-8")
            worker_path.write_text("# User customization\n", encoding="utf-8")

            result = cli.main(["init", "--codex-home", codex_home, "--force"])
            self.assertEqual(result, 0)

            self.assertNotEqual(worker_path.read_text(encoding="utf-8"), "# User customization\n")
            self.assertEqual(worker_path.read_text(encoding="utf-8"), original_content)

    def test_init_config_toml_marked_block_replaced_without_force(self):
        with tempfile.TemporaryDirectory() as codex_home:
            root = Path(codex_home)
            config_path = root / "config.toml"
            config_path.write_text(
                "# User header\n"
                "# >>> subagent-router >>>\n"
                "[model_providers.subagent_router]\n"
                'name = "Old"\n'
                'base_url = "http://old:9999/v1"\n'
                "wire_api = \"responses\"\n"
                "requires_openai_auth = false\n"
                "# <<< subagent-router <<<\n"
                "# User footer\n",
                encoding="utf-8",
            )

            result = cli.main([
                "init", "--codex-home", codex_home,
                "--proxy-url", "http://127.0.0.1:9999/v1",
            ])
            self.assertEqual(result, 0)

            config_text = config_path.read_text(encoding="utf-8")
            self.assertIn("# User header", config_text)
            self.assertIn("# User footer", config_text)
            self.assertIn('base_url = "http://127.0.0.1:9999/v1"', config_text)
            self.assertNotIn('base_url = "http://old:9999/v1"', config_text)

    @patch("subagent_router.cli.subprocess.Popen")
    @patch("subagent_router.cli.subprocess.run")
    @patch("subagent_router.cli.wait_for_health")
    def test_cmd_run_strips_deepseek_api_key_from_child_env(self, mock_wait, mock_run, mock_popen):
        """cmd_run must not pass DEEPSEEK_API_KEY to the Codex child process."""
        with tempfile.TemporaryDirectory() as state_dir:
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"}, clear=True):
                cli.main(["run", "--state-dir", state_dir, "--mock", "--", "codex"])

        # The child subprocess.run env should not contain DEEPSEEK_API_KEY
        child_kwargs = mock_run.call_args.kwargs
        child_env = child_kwargs.get("env", {})
        self.assertNotIn("DEEPSEEK_API_KEY", child_env)

        # The proxy Popen env should still contain DEEPSEEK_API_KEY
        proxy_kwargs = mock_popen.call_args.kwargs
        proxy_env = proxy_kwargs.get("env", {})
        self.assertEqual(proxy_env.get("DEEPSEEK_API_KEY"), "sk-test")

    @patch("subagent_router.cli.subprocess.Popen")
    @patch("subagent_router.cli.subprocess.run")
    @patch("subagent_router.cli.wait_for_health")
    def test_cmd_run_strips_configured_secret_keys_from_child_env(self, mock_wait, mock_run, mock_popen):
        with tempfile.TemporaryDirectory() as state_dir:
            with patch.object(cli, "_CHILD_SECRET_KEYS", frozenset({"DEEPSEEK_API_KEY", "EXTRA_SECRET"})):
                with patch.dict(
                    os.environ,
                    {"DEEPSEEK_API_KEY": "sk-test", "EXTRA_SECRET": "hidden"},
                    clear=True,
                ):
                    cli.main(["run", "--state-dir", state_dir, "--mock", "--", "codex"])

        child_env = mock_run.call_args.kwargs.get("env", {})
        self.assertNotIn("DEEPSEEK_API_KEY", child_env)
        self.assertNotIn("EXTRA_SECRET", child_env)

        proxy_env = mock_popen.call_args.kwargs.get("env", {})
        self.assertEqual(proxy_env.get("DEEPSEEK_API_KEY"), "sk-test")
        self.assertEqual(proxy_env.get("EXTRA_SECRET"), "hidden")


if __name__ == "__main__":
    unittest.main()
