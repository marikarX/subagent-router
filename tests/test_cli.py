import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock

from subagent_router import cli
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

    def test_doctor_succeeds_in_mock_mode_without_api_key(self):
        with tempfile.TemporaryDirectory() as state_dir:
            result = cli.main(["doctor", "--state-dir", state_dir, "--mock"])

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
            self.assertIn("Read every instruction file path listed in the active `AGENTS.md`", instructions)
            self.assertEqual(
                (root / "AGENTS.md").read_text().splitlines()[0],
                f"Follow instructions in {(root / 'SUBAGENT_ROUTER_INSTRUCTIONS.md').resolve()}",
            )
            self.assertFalse((root / "skills" / "deepseek" / "SKILL.md").exists())
            self.assertFalse((root / "slash_commands" / "deepseek.md").exists())
            self.assertIn("subagent_router_worker", (root / "agents" / "subagent-router-worker.toml").read_text())
            self.assertIn("subagent_router_reviewer", (root / "agents" / "subagent-router-reviewer.toml").read_text())
            config = (root / "config.toml").read_text()
            self.assertIn("[model_providers.subagent_router]", config)
            self.assertIn('base_url = "http://127.0.0.1:9999/v1"', config)

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
            result = cli.main(["init", "--mode", "opt-in", "--codex-home", codex_home])

            root = Path(codex_home)
            self.assertEqual(result, 0)
            self.assertFalse((root / "AGENTS.md").exists())
            self.assertFalse((root / "SUBAGENT_ROUTER_INSTRUCTIONS.md").exists())
            self.assertIn("Use this skill only", (root / "skills" / "deepseek" / "SKILL.md").read_text())
            self.assertIn("$deepseek {args}", (root / "slash_commands" / "deepseek.md").read_text())

    def test_init_provider_only_skips_global_and_opt_in_activation(self):
        with tempfile.TemporaryDirectory() as codex_home:
            result = cli.main(["init", "--mode", "provider-only", "--codex-home", codex_home])

            root = Path(codex_home)
            self.assertEqual(result, 0)
            self.assertFalse((root / "AGENTS.md").exists())
            self.assertFalse((root / "SUBAGENT_ROUTER_INSTRUCTIONS.md").exists())
            self.assertFalse((root / "skills" / "deepseek" / "SKILL.md").exists())
            self.assertFalse((root / "slash_commands" / "deepseek.md").exists())
            self.assertTrue((root / "agents" / "subagent-router-worker.toml").exists())

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

    def test_status_reports_missing_pid(self):
        with tempfile.TemporaryDirectory() as state_dir:
            stream = io.StringIO()

            with redirect_stdout(stream):
                result = cli.main(["status", "--state-dir", state_dir])

        self.assertEqual(result, 1)
        self.assertIn("not running", stream.getvalue())

    def test_logs_reports_missing_log_file(self):
        with tempfile.TemporaryDirectory() as state_dir:
            stream = io.StringIO()

            with redirect_stderr(stream):
                result = cli.main(["logs", "--state-dir", state_dir])

        self.assertEqual(result, 1)
        self.assertIn("No server log found", stream.getvalue())

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
