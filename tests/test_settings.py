import json
import os
import tempfile
import unittest
from dataclasses import replace
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

    def test_config_file_loads_providers_routes_and_budgets(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as state_dir:
            config_dir = Path(home) / ".config" / "subagent-router"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                f"""
state_dir = "{state_dir}"
dry_run = true

[defaults]
provider = "openai-compatible"
fallback_providers = ["deepseek"]

[providers.openai-compatible]
type = "openai-compatible"
kind = "cloud"
base_url = "http://localhost:9001/v1"
api_key = "sk-openai-compat"
model = "compat-model"

[providers.openai-compatible.capabilities]
context_window = 64000
tool_support = true
input_cost_per_million = 1.0
output_cost_per_million = 2.0

[routes.cheap-review]
provider = "openai-compatible"
model = "cheap-model"
fallback_providers = ["deepseek"]

[budgets]
max_cost_per_task = 0.25
max_tokens_per_task = 1000
mode = "hard-stop"

[security]
provider_allowlist = ["deepseek"]
provider_denylist = ["openai-compatible"]
""",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"HOME": home}, clear=True):
                settings = Settings.from_env()

        self.assertEqual(settings.provider, "openai-compatible")
        self.assertEqual(settings.fallback_providers, ["deepseek"])
        self.assertEqual(settings.providers["openai-compatible"].base_url, "http://localhost:9001/v1")
        self.assertEqual(settings.providers["openai-compatible"].capabilities.context_window, 64000)
        self.assertEqual(settings.routing_policies["cheap-review"]["model"], "cheap-model")
        self.assertEqual(settings.max_cost_per_task, 0.25)
        self.assertEqual(settings.max_tokens_per_task, 1000)
        self.assertEqual(settings.budget_mode, "hard-stop")
        self.assertEqual(settings.allowed_providers, ["deepseek"])
        self.assertEqual(settings.denied_providers, ["openai-compatible"])
        self.assertTrue(settings.dry_run)

    def test_providers_deepseek_block_configures_legacy_deepseek_provider(self):
        with tempfile.TemporaryDirectory() as home:
            config_dir = Path(home) / ".config" / "subagent-router"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                """
[providers.deepseek]
type = "deepseek"
base_url = "http://localhost:9002/v1"
api_key = "sk-deepseek"
model = "deepseek-custom"

[providers.deepseek.capabilities]
context_window = 32000
input_cost_per_million = 0.5
output_cost_per_million = 1.5
""",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"HOME": home}, clear=True):
                settings = Settings.from_env()

        self.assertEqual(settings.deepseek_base_url, "http://localhost:9002/v1")
        self.assertEqual(settings.deepseek_api_key, "sk-deepseek")
        self.assertEqual(settings.deepseek_model, "deepseek-custom")
        self.assertEqual(settings.providers["deepseek"].capabilities.context_window, 32000)
        self.assertEqual(settings.providers["deepseek"].capabilities.input_cost_per_million, 0.5)

    def test_provider_env_overrides_config_file_default_provider(self):
        with tempfile.TemporaryDirectory() as home:
            config_dir = Path(home) / ".config" / "subagent-router"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                """
[defaults]
provider = "ollama"
""",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"HOME": home, "SUBAGENT_ROUTER_PROVIDER": "deepseek"}, clear=True):
                settings = Settings.from_env()

        self.assertEqual(settings.provider, "deepseek")

    def test_daily_budget_settings_from_env(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {
                "HOME": home,
                "SUBAGENT_ROUTER_MAX_COST_PER_DAY": "2.50",
                "SUBAGENT_ROUTER_MAX_TOKENS_PER_DAY": "500000",
            }, clear=True):
                settings = Settings.from_env()
        self.assertEqual(settings.max_cost_per_day, 2.5)
        self.assertEqual(settings.max_tokens_per_day, 500000)

    def test_daily_budget_settings_from_config(self):
        with tempfile.TemporaryDirectory() as state_dir, tempfile.TemporaryDirectory() as home:
            config_dir = Path(home) / ".config" / "subagent-router"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                f"""
state_dir = "{state_dir}"

[budgets]
max_cost_per_day = 5.0
max_tokens_per_day = 1000000
max_cost_per_session = 1.0
max_tokens_per_session = 200000
""",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"HOME": home}, clear=True):
                settings = Settings.from_env()
        self.assertEqual(settings.max_cost_per_day, 5.0)
        self.assertEqual(settings.max_tokens_per_day, 1000000)
        self.assertEqual(settings.max_cost_per_session, 1.0)
        self.assertEqual(settings.max_tokens_per_session, 200000)

    def test_session_budget_settings_from_env(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {
                "HOME": home,
                "SUBAGENT_ROUTER_MAX_COST_PER_SESSION": "0.75",
                "SUBAGENT_ROUTER_MAX_TOKENS_PER_SESSION": "100000",
            }, clear=True):
                settings = Settings.from_env()
        self.assertEqual(settings.max_cost_per_session, 0.75)
        self.assertEqual(settings.max_tokens_per_session, 100000)

    def test_daily_budget_as_env(self):
        settings = Settings(max_cost_per_day=3.0, max_tokens_per_day=600000)
        env = settings.as_env()
        self.assertEqual(env.get("SUBAGENT_ROUTER_MAX_COST_PER_DAY"), "3.0")
        self.assertEqual(env.get("SUBAGENT_ROUTER_MAX_TOKENS_PER_DAY"), "600000")

    def test_session_budget_as_env(self):
        settings = Settings(max_cost_per_session=2.0, max_tokens_per_session=300000)
        env = settings.as_env()
        self.assertEqual(env.get("SUBAGENT_ROUTER_MAX_COST_PER_SESSION"), "2.0")
        self.assertEqual(env.get("SUBAGENT_ROUTER_MAX_TOKENS_PER_SESSION"), "300000")

    def test_daily_budget_defaults_to_none(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertIsNone(settings.max_cost_per_day)
        self.assertIsNone(settings.max_tokens_per_day)
        self.assertIsNone(settings.max_cost_per_session)
        self.assertIsNone(settings.max_tokens_per_session)

    def test_save_runtime_config_filters_null_pricing_values(self):
        with tempfile.TemporaryDirectory() as state_dir:
            settings = Settings.from_env({"SUBAGENT_ROUTER_STATE_DIR": state_dir})
            provider = settings.providers["deepseek"]
            settings = replace(
                settings,
                providers={
                    **settings.providers,
                    "deepseek": replace(
                        provider,
                        model_pricing={
                            "custom-model": {
                                "input_cost_per_million": 0.0,
                                "output_cost_per_million": None,
                                "cached_input_cost_per_million": 0.01,
                            }
                        },
                        explorer_model="deepseek-v4-flash",
                        worker_model=None,
                        reviewer_model="deepseek-v4-pro",
                    ),
                },
            )

            settings.save_runtime_config()

            data = json.loads((Path(state_dir) / "runtime_config.json").read_text(encoding="utf-8"))
            deepseek = data["providers"]["deepseek"]
            self.assertEqual(deepseek["explorer_model"], "deepseek-v4-flash")
            self.assertNotIn("worker_model", deepseek)
            self.assertEqual(deepseek["reviewer_model"], "deepseek-v4-pro")
            self.assertNotIn("out", deepseek["model_pricing"]["custom-model"])
            self.assertEqual(deepseek["model_pricing"]["custom-model"]["in"], 0.0)
            self.assertEqual(deepseek["model_pricing"]["custom-model"]["cached"], 0.01)


    # ---------- ProviderPrediction tests ----------

    def test_provider_predictions_from_config(self):
        with tempfile.TemporaryDirectory() as home:
            config_dir = Path(home) / ".config" / "subagent-router"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                """
[predictions.deepseek]
reliability_score = 0.95
latency_p50_ms = 250.0
latency_p99_ms = 2000.0
capability_score = 0.85
budget_prediction_usd = 0.002
budget_prediction_tokens = 4000

[predictions.ollama]
reliability_score = 0.99
capability_score = 0.6
""",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"HOME": home}, clear=True):
                settings = Settings.from_env()

        self.assertIn("deepseek", settings.provider_predictions)
        ds = settings.provider_predictions["deepseek"]
        self.assertEqual(ds.reliability_score, 0.95)
        self.assertEqual(ds.latency_p50_ms, 250.0)
        self.assertEqual(ds.latency_p99_ms, 2000.0)
        self.assertEqual(ds.capability_score, 0.85)
        self.assertEqual(ds.budget_prediction_usd, 0.002)
        self.assertEqual(ds.budget_prediction_tokens, 4000)
        self.assertIn("ollama", settings.provider_predictions)
        self.assertEqual(settings.provider_predictions["ollama"].reliability_score, 0.99)
        self.assertIsNone(settings.provider_predictions["ollama"].latency_p50_ms)

    def test_provider_predictions_defaults_empty(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {"HOME": home}, clear=True):
                settings = Settings.from_env()
        self.assertEqual(settings.provider_predictions, {})

    # ---------- Spend limit tests ----------

    def test_max_spend_per_task_from_env(self):
        with patch.dict(os.environ, {"SUBAGENT_ROUTER_MAX_SPEND_PER_TASK": "0.50"}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.max_spend_per_task, 0.5)

    def test_max_spend_per_day_from_env(self):
        with patch.dict(os.environ, {"SUBAGENT_ROUTER_MAX_SPEND_PER_DAY": "10.0"}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.max_spend_per_day, 10.0)

    def test_max_spend_per_session_from_env(self):
        with patch.dict(os.environ, {"SUBAGENT_ROUTER_MAX_SPEND_PER_SESSION": "2.0"}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.max_spend_per_session, 2.0)

    def test_max_spend_defaults_to_none(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertIsNone(settings.max_spend_per_task)
        self.assertIsNone(settings.max_spend_per_day)
        self.assertIsNone(settings.max_spend_per_session)
        self.assertEqual(settings.max_spend_per_provider, {})
        self.assertEqual(settings.max_spend_per_model, {})

    def test_max_spend_from_config(self):
        with tempfile.TemporaryDirectory() as home:
            config_dir = Path(home) / ".config" / "subagent-router"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                """
[budgets]
max_spend_per_task = 0.75
max_spend_per_day = 15.0
max_spend_per_session = 3.0
provider_max_spend_per_task.deepseek = 0.5
model_max_spend_per_task.deepseek-chat = 0.25
""",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"HOME": home}, clear=True):
                settings = Settings.from_env()
        self.assertEqual(settings.max_spend_per_task, 0.75)
        self.assertEqual(settings.max_spend_per_day, 15.0)
        self.assertEqual(settings.max_spend_per_session, 3.0)
        self.assertEqual(settings.max_spend_per_provider.get("deepseek"), 0.5)
        self.assertEqual(settings.max_spend_per_model.get("deepseek-chat"), 0.25)

    def test_max_spend_as_env(self):
        settings = Settings(max_spend_per_task=1.0, max_spend_per_day=20.0, max_spend_per_session=5.0)
        env = settings.as_env()
        self.assertEqual(env.get("SUBAGENT_ROUTER_MAX_SPEND_PER_TASK"), "1.0")
        self.assertEqual(env.get("SUBAGENT_ROUTER_MAX_SPEND_PER_DAY"), "20.0")
        self.assertEqual(env.get("SUBAGENT_ROUTER_MAX_SPEND_PER_SESSION"), "5.0")

    # ---------- Debug mode tests ----------

    def test_debug_mode_from_env(self):
        with patch.dict(os.environ, {"SUBAGENT_ROUTER_DEBUG": "1"}, clear=True):
            settings = Settings.from_env()
        self.assertTrue(settings.debug_mode)
        self.assertTrue(any("debug mode is enabled" in warning for warning in settings.config_warnings))

    def test_debug_mode_default_false(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertFalse(settings.debug_mode)

    def test_debug_mode_as_env(self):
        settings = Settings(debug_mode=True)
        env = settings.as_env()
        self.assertEqual(env.get("SUBAGENT_ROUTER_DEBUG"), "1")

    # ---------- Config permission warnings tests ----------

    def test_config_permission_warnings_returns_list(self):
        from subagent_router.settings import config_permission_warnings
        warnings = config_permission_warnings({"HOME": "/nonexistent"})
        self.assertIsInstance(warnings, list)

    # ---------- Provider health metadata field test ----------

    def test_provider_health_defaults_empty(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.provider_health, {})



if __name__ == "__main__":
    unittest.main()
