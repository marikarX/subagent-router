import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from subagent_router.settings import Settings


class SettingsTests(unittest.TestCase):
    def test_defaults_resolve_paths_under_home_state_dir(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {"HOME": home}, clear=True):
                settings = Settings.from_env()

        expected_state_dir = Path(home) / ".local" / "state" / "subagent-router"
        self.assertEqual(settings.state_dir, expected_state_dir.resolve())
        self.assertEqual(settings.log_dir, expected_state_dir / "logs" / "client_payloads")
        self.assertEqual(settings.activity_file, expected_state_dir / "logs" / "activity.json")
        self.assertEqual(settings.session_mirror_file, expected_state_dir / "logs" / "session_mirror.json")
        self.assertEqual(settings.provider_error_log_dir, expected_state_dir / "logs" / "provider_errors")

    def test_xdg_state_dir_takes_precedence(self):
        with tempfile.TemporaryDirectory() as xdg_state:
            with patch.dict(os.environ, {"XDG_STATE_HOME": xdg_state}, clear=True):
                settings = Settings.from_env()

        self.assertEqual(settings.state_dir, Path(xdg_state) / "subagent-router")

    def test_relative_path_overrides_resolve_under_state_dir(self):
        with tempfile.TemporaryDirectory() as state_dir:
            with patch.dict(
                os.environ,
                {
                    "SUBAGENT_ROUTER_STATE_DIR": state_dir,
                    "SUBAGENT_ROUTER_LOG_DIR": "payloads",
                    "SUBAGENT_ROUTER_ACTIVITY_FILE": "activity.json",
                    "SUBAGENT_ROUTER_SESSION_MIRROR_FILE": "mirror/session.json",
                    "SUBAGENT_ROUTER_PROVIDER_ERROR_LOG_DIR": "errors",
                },
                clear=True,
            ):
                settings = Settings.from_env()

        self.assertEqual(settings.log_dir, Path(state_dir) / "payloads")
        self.assertEqual(settings.activity_file, Path(state_dir) / "activity.json")
        self.assertEqual(settings.session_mirror_file, Path(state_dir) / "mirror" / "session.json")
        self.assertEqual(settings.provider_error_log_dir, Path(state_dir) / "errors")

    def test_user_paths_expand_and_absolute_paths_pass_through(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as absolute_dir:
            with patch.dict(
                os.environ,
                {
                    "HOME": home,
                    "SUBAGENT_ROUTER_STATE_DIR": "~/state-root",
                    "SUBAGENT_ROUTER_LOG_DIR": absolute_dir,
                },
                clear=True,
            ):
                settings = Settings.from_env()

        self.assertEqual(settings.state_dir, Path(home) / "state-root")
        self.assertEqual(settings.log_dir, Path(absolute_dir))

    def test_required_env_settings_are_loaded(self):
        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": "sk-test",
                "DEEPSEEK_BASE_URL": "http://localhost:9000/v1",
                "DEEPSEEK_MODEL": "deepseek-v4-pro",
                "DEEPSEEK_SEND_PARALLEL_TOOL_CALLS": "1",
                "DEEPSEEK_PROXY_MOCK": "1",
                "DEEPSEEK_ALLOW_APPLY_PATCH": "1",
                "SUBAGENT_ROUTER_HOST": "0.0.0.0",
                "SUBAGENT_ROUTER_PORT": "9999",
                "SUBAGENT_ROUTER_TRACE": "1",
            },
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.deepseek_api_key, "sk-test")
        self.assertEqual(settings.deepseek_base_url, "http://localhost:9000/v1")
        self.assertEqual(settings.deepseek_model, "deepseek-v4-pro")
        self.assertTrue(settings.send_parallel_tool_calls)
        self.assertTrue(settings.mock_deepseek)
        self.assertTrue(settings.allow_apply_patch)
        self.assertEqual(settings.host, "0.0.0.0")
        self.assertEqual(settings.port, 9999)
        self.assertTrue(settings.trace_enabled)

    def test_as_env_excludes_deepseek_api_key_by_default(self):
        settings = Settings(
            deepseek_api_key="sk-test",
            deepseek_base_url="http://localhost:9000/v1",
        )

        env = settings.as_env()

        self.assertNotIn("DEEPSEEK_API_KEY", env)
        self.assertEqual(env["DEEPSEEK_BASE_URL"], "http://localhost:9000/v1")
        self.assertIn("SUBAGENT_ROUTER_STATE_DIR", env)

    def test_legacy_codex_proxy_env_aliases_still_work(self):
        with tempfile.TemporaryDirectory() as state_dir:
            settings = Settings.from_env(
                {
                    "CODEX_PROXY_STATE_DIR": state_dir,
                    "CODEX_PROXY_LOG_DIR": "payloads",
                    "CODEX_PROXY_HOST": "0.0.0.0",
                    "CODEX_PROXY_PORT": "9999",
                    "CODEX_PROXY_TRACE": "1",
                }
            )

        self.assertEqual(settings.state_dir, Path(state_dir).resolve())
        self.assertEqual(settings.log_dir, Path(state_dir) / "payloads")
        self.assertEqual(settings.host, "0.0.0.0")
        self.assertEqual(settings.port, 9999)
        self.assertTrue(settings.trace_enabled)

    def test_as_env_includes_secrets_when_opted_in(self):
        settings = Settings(deepseek_api_key="sk-test")

        env = settings.as_env(include_secrets=True)

        self.assertEqual(env["DEEPSEEK_API_KEY"], "sk-test")

    def test_as_env_omit_api_key_when_none(self):
        settings = Settings(deepseek_api_key=None)

        self.assertNotIn("DEEPSEEK_API_KEY", settings.as_env())
        self.assertNotIn("DEEPSEEK_API_KEY", settings.as_env(include_secrets=True))


if __name__ == "__main__":
    unittest.main()
