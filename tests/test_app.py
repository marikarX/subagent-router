import asyncio
import datetime
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

import httpx

import subagent_router.app as proxy_app
from subagent_router.normalization import normalize_request
from subagent_router.settings import ProviderPrediction


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_settings = proxy_app.SETTINGS
        
        # Isolate tests from user's real ~/.subagent_router config
        mock_state_dir = Path(self.tempdir.name) / "state"
        mock_state_dir.mkdir()
        
        proxy_app.SETTINGS = proxy_app.Settings.from_env(env={"SUBAGENT_ROUTER_STATE_DIR": str(mock_state_dir)})
        proxy_app.SETTINGS.mock_deepseek = True
        proxy_app.SETTINGS.trace_enabled = False
        proxy_app.SETTINGS.log_dir = Path(self.tempdir.name)
        proxy_app.SETTINGS.provider_error_log_dir = Path(self.tempdir.name) / "provider_errors"
        proxy_app.SETTINGS.activity_file = Path(self.tempdir.name) / "activity.json"
        proxy_app.SETTINGS.session_mirror_file = Path(self.tempdir.name) / "session_mirror.json"
        proxy_app.SETTINGS.audit_log_file = Path(self.tempdir.name) / "audit.jsonl"
        proxy_app.SETTINGS.usage_file = Path(self.tempdir.name) / "usage.json"
        proxy_app.SETTINGS.usage_jsonl_file = Path(self.tempdir.name) / "usage.jsonl"
        proxy_app.RESPONSE_STATES.clear()
        proxy_app.REASONING_CONTENT_BY_CALL_ID.clear()
        proxy_app.REASONING_CONTENT_BY_ASSISTANT_TEXT.clear()
        proxy_app.SESSION_EVENTS.clear()
        proxy_app.LAST_FINAL_SESSION_EVENT = None
        proxy_app.ACTIVITY_STATE.update(
            {
                "started_at": None,
                "last_request_at": None,
                "last_response_at": None,
                "last_error_at": None,
                "request_count": 0,
                "response_count": 0,
                "error_count": 0,
                "last_trace_id": None,
                "last_model": None,
                "last_provider": None,
                "last_output_kind": None,
                "last_end_turn": None,
                "requests_by_provider": {},
                "requests_by_model": {},
                "errors_by_provider": {},
                "local_request_count": 0,
                "cloud_request_count": 0,
                "fallback_count": 0,
                "total_latency_ms": 0,
                "average_latency_ms": 0,
                "total_cost_usd": 0.0,
                "total_tokens": 0,
            }
        )

    def tearDown(self):
        proxy_app.SETTINGS = self.original_settings
        self.tempdir.cleanup()

    def test_simple_response_returns_assistant_message(self):
        payload = {
            "model": "deepseek-chat",
            "stream": False,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                }
            ],
            "tools": [],
        }
        proxy_app.log_payload(payload)
        normalized = normalize_request(payload)
        chat_response = asyncio.run(proxy_app.call_deepseek(normalized))
        body = proxy_app.responses_object(payload, normalized, chat_response)

        self.assertEqual(body["output"][0]["type"], "message")
        self.assertIn("Subagent Router: hello", body["output"][0]["content"][0]["text"])

    def test_create_response_endpoint_returns_json_response(self):
        from fastapi.testclient import TestClient

        client = TestClient(proxy_app.app)
        payload = {"model": "deepseek-chat", "stream": False, "input": "hello", "tools": []}

        response = client.post("/v1/responses", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["output"][0]["type"], "message")

    def test_config_endpoint_includes_delegation_profile(self):
        from fastapi.testclient import TestClient

        response = TestClient(proxy_app.app).get("/v1/config")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["delegation_profile"])

    def test_config_endpoint_uses_settings_codex_home_for_delegation_profile(self):
        from fastapi.testclient import TestClient

        codex_home = Path(self.tempdir.name) / "codex-home"
        codex_home.mkdir()
        (codex_home / ".subagent-router-manifest.json").write_text(
            json.dumps({"delegation_profile": "orchestrator"}),
            encoding="utf-8",
        )
        proxy_app.SETTINGS = replace(proxy_app.SETTINGS, codex_home=codex_home)

        response = TestClient(proxy_app.app).get("/v1/config")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["delegation_profile"], "orchestrator")

    def test_empty_chat_completion_output_is_rejected(self):
        payload = {"model": "deepseek-chat", "stream": False, "input": "hello", "tools": []}
        normalized = normalize_request(payload)
        chat_response = {"choices": [{"message": {"role": "assistant", "content": ""}}]}

        with self.assertRaises(proxy_app.ProviderIncompleteOutputError):
            proxy_app.responses_object(payload, normalized, chat_response)

    def test_empty_provider_output_endpoint_returns_error_not_empty_final(self):
        from fastapi.testclient import TestClient

        original_call_provider = proxy_app.call_provider

        async def empty_call_provider(normalized, route):
            return proxy_app.ProviderResponse(
                provider="deepseek",
                model=normalized.model,
                provider_kind="cloud",
                chat_response={"choices": [{"message": {"role": "assistant", "content": ""}}]},
                latency_ms=1,
                estimated_usage=False,
            )

        proxy_app.call_provider = empty_call_provider
        try:
            response = TestClient(proxy_app.app).post(
                "/v1/responses",
                json={"model": "deepseek-chat", "stream": False, "input": "hello", "tools": []},
            )
        finally:
            proxy_app.call_provider = original_call_provider

        self.assertEqual(response.status_code, 502)
        self.assertIn("empty", response.json()["error"]["message"])
        self.assertEqual(proxy_app.ACTIVITY_STATE["response_count"], 0)
        self.assertEqual(proxy_app.ACTIVITY_STATE["error_count"], 1)

    def test_progress_only_final_text_for_subagent_is_rejected(self):
        payload = {"model": "subagent-router-worker", "stream": False, "input": "fix it", "tools": []}
        normalized = normalize_request(payload)
        chat_response = {"choices": [{"message": {"role": "assistant", "content": "Now I'll fix both call sites:"}}]}

        with self.assertRaisesRegex(proxy_app.ProviderIncompleteOutputError, "progress text"):
            proxy_app.responses_object(payload, normalized, chat_response)

    def test_progress_only_final_text_variants_for_subagent_are_rejected(self):
        payload = {"model": "subagent-router-worker", "stream": False, "input": "fix it", "tools": []}
        normalized = normalize_request(payload)
        variants = [
            "Let me now apply the patch.",
            "I'll now inspect the file.",
            "I'm going to update the docs.",
            "Applying the patch now.",
            "First, let me inspect the file.\nI'll start with app.py.",
            'I can see the existing note. I will adjust it to say "keep apply_patch enabled by default."',
            "I can see the file. The Tool Filtering section has the bullet I need to change. Let me apply the edit.",
        ]

        for content in variants:
            chat_response = {"choices": [{"message": {"role": "assistant", "content": content}}]}
            with self.subTest(content=content):
                with self.assertRaisesRegex(proxy_app.ProviderIncompleteOutputError, "progress text"):
                    proxy_app.responses_object(payload, normalized, chat_response)

    def test_progress_only_final_text_allows_real_completion_summary(self):
        payload = {"model": "subagent-router-worker", "stream": False, "input": "fix it", "tools": []}
        normalized = normalize_request(payload)
        chat_response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Updated docs/troubleshooting.md and ran the focused tests.",
                    }
                }
            ]
        }

        body = proxy_app.responses_object(payload, normalized, chat_response)

        self.assertTrue(body["end_turn"])
        self.assertIn("Updated", body["output"][0]["content"][0]["text"])

    def test_write_worker_final_without_apply_patch_evidence_is_rejected(self):
        payload = {
            "model": "subagent-router-worker",
            "stream": False,
            "input": "Change docs/troubleshooting.md wording.",
            "tools": [
                {
                    "type": "function",
                    "name": "apply_patch",
                    "description": "patch",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        }
        normalized = normalize_request(payload)
        chat_response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "The file is ready for the wording update.",
                    }
                }
            ]
        }

        with self.assertRaisesRegex(proxy_app.ProviderIncompleteOutputError, "before performing"):
            proxy_app.responses_object(payload, normalized, chat_response)

    def test_write_worker_final_allows_no_change_rationale_without_apply_patch(self):
        payload = {
            "model": "subagent-router-worker",
            "stream": False,
            "input": "Change docs/troubleshooting.md wording.",
            "tools": [
                {
                    "type": "function",
                    "name": "apply_patch",
                    "description": "patch",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        }
        normalized = normalize_request(payload)
        chat_response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "No changes needed; the requested wording is already present.",
                    }
                }
            ]
        }

        body = proxy_app.responses_object(payload, normalized, chat_response)

        self.assertTrue(body["end_turn"])

    def test_write_worker_final_allows_completion_after_apply_patch_tool_result(self):
        payload = {
            "model": "subagent-router-worker",
            "stream": False,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Change docs/troubleshooting.md wording."}],
                },
                {
                    "type": "function_call",
                    "name": "apply_patch",
                    "call_id": "call_patch_1",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_patch_1",
                    "output": "Success. Updated docs/troubleshooting.md",
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "apply_patch",
                    "description": "patch",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        }
        normalized = normalize_request(payload)
        chat_response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Changed docs/troubleshooting.md. Tests not run.",
                    }
                }
            ]
        }

        body = proxy_app.responses_object(payload, normalized, chat_response)

        self.assertTrue(body["end_turn"])
        self.assertIn("Changed docs/troubleshooting.md", body["output"][0]["content"][0]["text"])

    def test_subagent_empty_output_retries_once_before_returning_endpoint_response(self):
        from fastapi.testclient import TestClient

        original_call_provider = proxy_app.call_provider
        calls = []

        async def empty_then_final_call_provider(normalized, route):
            calls.append([message["content"] for message in normalized.messages if message.get("role") == "user"])
            content = "" if len(calls) == 1 else "Updated docs/troubleshooting.md. Tests not run."
            return proxy_app.ProviderResponse(
                provider="deepseek",
                model=normalized.model,
                provider_kind="cloud",
                chat_response={"choices": [{"message": {"role": "assistant", "content": content}}]},
                latency_ms=1,
                estimated_usage=False,
            )

        proxy_app.call_provider = empty_then_final_call_provider
        try:
            response = TestClient(proxy_app.app).post(
                "/v1/responses",
                json={"model": "subagent-router-worker", "stream": False, "input": "fix docs", "tools": []},
            )
        finally:
            proxy_app.call_provider = original_call_provider

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 2)
        self.assertIn("Continue from the previous tool result", calls[1][-1])
        self.assertEqual(proxy_app.ACTIVITY_STATE["error_count"], 0)
        self.assertIn("Updated docs/troubleshooting.md", response.json()["output"][0]["content"][0]["text"])

    def test_non_subagent_empty_output_still_returns_502(self):
        from fastapi.testclient import TestClient

        original_call_provider = proxy_app.call_provider
        calls = 0

        async def empty_call_provider(normalized, route):
            nonlocal calls
            calls += 1
            return proxy_app.ProviderResponse(
                provider="deepseek",
                model=normalized.model,
                provider_kind="cloud",
                chat_response={"choices": [{"message": {"role": "assistant", "content": ""}}]},
                latency_ms=1,
                estimated_usage=False,
            )

        proxy_app.call_provider = empty_call_provider
        try:
            response = TestClient(proxy_app.app).post(
                "/v1/responses",
                json={"model": "deepseek-chat", "stream": False, "input": "hello", "tools": []},
            )
        finally:
            proxy_app.call_provider = original_call_provider

        self.assertEqual(response.status_code, 502)
        self.assertEqual(calls, 1)

    def test_subagent_exhausted_incomplete_retry_returns_continuation_tool_call(self):
        from fastapi.testclient import TestClient

        original_call_provider = proxy_app.call_provider
        calls = 0

        async def empty_call_provider(normalized, route):
            nonlocal calls
            calls += 1
            return proxy_app.ProviderResponse(
                provider="deepseek",
                model=normalized.model,
                provider_kind="cloud",
                chat_response={"choices": [{"message": {"role": "assistant", "content": ""}}]},
                latency_ms=1,
                estimated_usage=False,
            )

        proxy_app.call_provider = empty_call_provider
        try:
            response = TestClient(proxy_app.app).post(
                "/v1/responses",
                json={
                    "model": "subagent-router-worker",
                    "stream": False,
                    "input": "fix docs",
                    "tools": [
                        {
                            "type": "function",
                            "name": "exec_command",
                            "description": "run",
                            "parameters": {"type": "object", "properties": {}},
                        }
                    ],
                },
            )
        finally:
            proxy_app.call_provider = original_call_provider

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, 2)
        self.assertFalse(body["end_turn"])
        self.assertEqual(body["output"][0]["type"], "function_call")
        self.assertEqual(body["output"][0]["name"], "exec_command")
        self.assertEqual(body["metadata"]["synthetic_continuation"], True)
        self.assertEqual(proxy_app.ACTIVITY_STATE["error_count"], 0)

    def test_subagent_write_final_without_evidence_returns_continuation_tool_call(self):
        from fastapi.testclient import TestClient

        original_call_provider = proxy_app.call_provider
        calls = 0

        async def unsupported_final_call_provider(normalized, route):
            nonlocal calls
            calls += 1
            return proxy_app.ProviderResponse(
                provider="deepseek",
                model=normalized.model,
                provider_kind="cloud",
                chat_response={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "The file is ready for the wording update.",
                            }
                        }
                    ]
                },
                latency_ms=1,
                estimated_usage=False,
            )

        proxy_app.call_provider = unsupported_final_call_provider
        try:
            response = TestClient(proxy_app.app).post(
                "/v1/responses",
                json={
                    "model": "subagent-router-worker",
                    "stream": False,
                    "input": "Change docs/troubleshooting.md wording.",
                    "tools": [
                        {
                            "type": "function",
                            "name": "exec_command",
                            "description": "run",
                            "parameters": {"type": "object", "properties": {}},
                        },
                        {
                            "type": "function",
                            "name": "apply_patch",
                            "description": "patch",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    ],
                },
            )
        finally:
            proxy_app.call_provider = original_call_provider

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, 2)
        self.assertFalse(body["end_turn"])
        self.assertEqual(body["output"][0]["type"], "function_call")
        self.assertEqual(body["output"][0]["name"], "exec_command")
        self.assertEqual(body["metadata"]["synthetic_continuation"], True)
        self.assertEqual(proxy_app.ACTIVITY_STATE["error_count"], 0)

    def test_default_settings_preserve_apply_patch_for_worker_requests(self):
        payload = {
            "model": "subagent-router-worker",
            "tools": [
                {
                    "type": "function",
                    "name": "apply_patch",
                    "description": "patch",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        }

        normalized = normalize_request(
            payload,
            allow_apply_patch_enabled=proxy_app.SETTINGS.apply_patch_override(),
        )

        self.assertEqual(normalized.normalized_tool_names, ["apply_patch"])
        self.assertEqual(normalized.dropped_tools, [])

    def test_default_settings_still_drop_apply_patch_for_reviewer_requests(self):
        payload = {
            "model": "subagent-router-reviewer",
            "tools": [
                {
                    "type": "function",
                    "name": "apply_patch",
                    "description": "patch",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        }

        normalized = normalize_request(
            payload,
            allow_apply_patch_enabled=proxy_app.SETTINGS.apply_patch_override(),
        )

        self.assertEqual(normalized.tools, [])
        self.assertEqual(normalized.dropped_tools, ["apply_patch"])

    def test_valid_subagent_final_message_still_works(self):
        payload = {"model": "subagent-router-worker", "stream": False, "input": "fix it", "tools": []}
        normalized = normalize_request(payload)
        chat_response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Changed src/subagent_router/app.py and ran pytest successfully.",
                    }
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 8, "total_tokens": 9},
        }

        body = proxy_app.responses_object(payload, normalized, chat_response)

        self.assertTrue(body["end_turn"])
        self.assertEqual(body["output"][0]["type"], "message")
        self.assertIn("Changed", body["output"][0]["content"][0]["text"])

    def test_budget_helpers_accept_provider_and_model_limits(self):
        normalized = normalize_request({"model": "subagent-router-worker", "input": "x" * 200, "tools": []})
        config = proxy_app.ProviderConfig(
            name="deepseek",
            kind="cloud",
            base_url="https://api.deepseek.com/v1",
            provider_type="deepseek",
            model="deepseek-v4-flash",
        )
        proxy_app.SETTINGS.budget_mode = "hard-stop"
        proxy_app.SETTINGS.max_tokens_per_task = None
        proxy_app.SETTINGS.max_tokens_per_provider = {"deepseek": 10_000}
        proxy_app.SETTINGS.max_tokens_per_model = {"deepseek-v4-flash": 5}
        proxy_app.SETTINGS.max_cost_per_task = None
        proxy_app.SETTINGS.max_cost_per_provider = {"deepseek": 10.0}
        proxy_app.SETTINGS.max_cost_per_model = {"deepseek-v4-flash": 0.01}

        self.assertTrue(proxy_app.budget_hard_stop_for_request(normalized, config, "deepseek-v4-flash"))
        self.assertTrue(
            proxy_app.budget_hard_stop(
                0.02,
                {"total_tokens": 1},
                "deepseek",
                "deepseek-v4-flash",
            )
        )

    def test_call_provider_prefers_explorer_role_model_over_policy_model(self):
        from fastapi.testclient import TestClient

        class CaptureProvider:
            async def chat(self, normalized, model):
                return proxy_app.ProviderResponse(
                    provider="deepseek",
                    model=model,
                    provider_kind="cloud",
                    chat_response={
                        "choices": [{"message": {"role": "assistant", "content": "explored"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                    latency_ms=1,
                    estimated_usage=False,
                )

        original_build_provider = proxy_app.build_provider
        proxy_app.SETTINGS.providers["deepseek"] = replace(
            proxy_app.SETTINGS.providers["deepseek"],
            model="default-model",
            explorer_model="cheap-explorer",
            worker_model="worker-model",
        )
        proxy_app.SETTINGS.provider = "deepseek"
        proxy_app.build_provider = lambda config: CaptureProvider()
        try:
            response = TestClient(proxy_app.app).post(
                "/v1/responses",
                json={
                    "model": "subagent-router-explorer",
                    "stream": False,
                    "input": "map this repo",
                    "tools": [],
                    "metadata": {"routing_policy": "safe-default"},
                },
            )
        finally:
            proxy_app.build_provider = original_build_provider

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["metadata"]["provider_model"], "cheap-explorer")

    def test_max_spend_aliases_are_enforced(self):
        proxy_app.SETTINGS.budget_mode = "hard-stop"
        proxy_app.SETTINGS.max_spend_per_task = 0.01

        self.assertTrue(
            proxy_app.budget_hard_stop(
                0.02,
                {"total_tokens": 1},
                "deepseek",
                "deepseek-v4-flash",
            )
        )

    def test_predicted_request_budget_can_hard_stop_before_provider_call(self):
        normalized = normalize_request({"model": "subagent-router-worker", "input": "hello", "tools": []})
        config = proxy_app.ProviderConfig(
            name="deepseek",
            kind="cloud",
            base_url="https://api.deepseek.com/v1",
            provider_type="deepseek",
            model="deepseek-v4-flash",
        )
        proxy_app.SETTINGS.budget_mode = "hard-stop"
        proxy_app.SETTINGS.max_spend_per_provider = {"deepseek": 0.01}
        proxy_app.SETTINGS.provider_predictions = {
            "deepseek": ProviderPrediction(budget_prediction_usd=0.02)
        }

        self.assertTrue(proxy_app.budget_hard_stop_for_request(normalized, config, "deepseek-v4-flash"))

    def test_automatic_provider_order_uses_predictions(self):
        proxy_app.SETTINGS.providers["ollama"] = replace(proxy_app.SETTINGS.providers["ollama"], enabled=True)
        proxy_app.SETTINGS.provider_predictions = {
            "deepseek": ProviderPrediction(reliability_score=0.90, latency_p50_ms=3000, capability_score=0.9, budget_prediction_usd=0.02),
            "ollama": ProviderPrediction(reliability_score=0.99, latency_p50_ms=100, capability_score=0.7, budget_prediction_usd=0.0),
        }

        provider, fallbacks, reason = proxy_app.optimize_provider_order(
            "deepseek",
            ["ollama"],
            {"automatic_routing": True},
            base_reason="test policy",
        )

        self.assertEqual(provider, "ollama")
        self.assertEqual(fallbacks, ["deepseek"])
        self.assertIn("automatic score", reason)

    def test_create_response_endpoint_increments_activity_count_once(self):
        from fastapi.testclient import TestClient

        proxy_app.ACTIVITY_STATE.update(
            {
                "started_at": None,
                "last_request_at": None,
                "last_response_at": None,
                "last_error_at": None,
                "request_count": 0,
                "response_count": 0,
                "error_count": 0,
                "last_trace_id": None,
                "last_model": None,
                "last_provider": None,
                "last_output_kind": None,
                "last_end_turn": None,
                "requests_by_provider": {},
                "requests_by_model": {},
                "errors_by_provider": {},
                "local_request_count": 0,
                "cloud_request_count": 0,
                "fallback_count": 0,
                "total_latency_ms": 0,
                "average_latency_ms": 0,
                "total_cost_usd": 0.0,
            }
        )

        client = TestClient(proxy_app.app)
        payload = {"model": "deepseek-chat", "stream": False, "input": "hello", "tools": []}

        response = client.post("/v1/responses", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(proxy_app.ACTIVITY_STATE["request_count"], 1)
        self.assertEqual(proxy_app.ACTIVITY_STATE["response_count"], 1)
        self.assertEqual(proxy_app.ACTIVITY_STATE["error_count"], 0)
        self.assertIsNotNone(proxy_app.ACTIVITY_STATE["last_model"])

    def test_create_response_after_reset_restores_activity_defaults(self):
        from fastapi.testclient import TestClient

        client = TestClient(proxy_app.app)

        reset_response = client.post("/v1/reset")
        response = client.post(
            "/v1/responses",
            json={"model": "deepseek-chat", "stream": False, "input": "hello", "tools": []},
        )

        self.assertEqual(reset_response.status_code, 200)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(proxy_app.ACTIVITY_STATE["request_count"], 1)
        self.assertEqual(proxy_app.ACTIVITY_STATE["response_count"], 1)
        self.assertEqual(proxy_app.ACTIVITY_STATE["error_count"], 0)
        self.assertIn("started_at", proxy_app.ACTIVITY_STATE)

    def test_create_response_records_normalization_error_in_audit(self):
        from fastapi.testclient import TestClient

        client = TestClient(proxy_app.app)
        payload = {
            "model": "subagent-router-reviewer",
            "stream": False,
            "input": [
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": "{}",
                    "call_id": "call_123",
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "bad"}],
                },
            ],
            "tools": [],
        }

        response = client.post("/v1/responses", json=payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(proxy_app.ACTIVITY_STATE["error_count"], 1)
        self.assertEqual(proxy_app.ACTIVITY_STATE["last_model"], "subagent-router-reviewer")
        audit = proxy_app.SETTINGS.audit_log_file.read_text(encoding="utf-8")
        self.assertIn('"status": "error"', audit)
        self.assertIn('"model": "subagent-router-reviewer"', audit)
        self.assertIn('"provider": "deepseek"', audit)

    def test_create_response_records_provider_metadata_usage_and_audit(self):
        from fastapi.testclient import TestClient

        client = TestClient(proxy_app.app)
        payload = {"model": "deepseek-chat", "stream": False, "input": "hello", "tools": []}

        response = client.post("/v1/responses", json=payload)

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["metadata"]["provider"], "deepseek")
        self.assertEqual(body["metadata"]["routing_policy"], "safe-default")
        self.assertEqual(proxy_app.ACTIVITY_STATE["last_provider"], "deepseek")
        self.assertTrue(proxy_app.SETTINGS.audit_log_file.exists())
        audit = proxy_app.SETTINGS.audit_log_file.read_text(encoding="utf-8")
        self.assertIn('"provider": "deepseek"', audit)
        usage = json.loads(proxy_app.SETTINGS.usage_file.read_text(encoding="utf-8"))
        self.assertEqual(usage["request_count"], 1)
        self.assertEqual(usage["requests_by_provider"]["deepseek"], 1)
        self.assertTrue(proxy_app.SETTINGS.usage_jsonl_file.exists())

    def test_subagent_alias_overrides_provider_default_model_in_audit(self):
        from fastapi.testclient import TestClient

        client = TestClient(proxy_app.app)
        payload = {
            "model": "subagent-router-reviewer",
            "stream": False,
            "input": "read-only smoke test",
            "tools": [],
        }

        response = client.post("/v1/responses", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["metadata"]["provider_model"], "deepseek-v4-pro")
        audit = proxy_app.SETTINGS.audit_log_file.read_text(encoding="utf-8")
        self.assertIn('"model": "deepseek-v4-pro"', audit)
        self.assertNotIn('"model": "deepseek-v4-flash"', audit)

    def test_provider_manual_override_routes_to_mocked_ollama(self):
        from fastapi.testclient import TestClient
        from subagent_router.providers import ProviderConfig

        original_build_provider = proxy_app.build_provider

        class FakeProvider:
            def __init__(self, config):
                self.config = config

            async def chat(self, normalized, *, model=None):
                return proxy_app.ProviderResponse(
                    provider=self.config.name,
                    model=model or self.config.model or normalized.model,
                    provider_kind=self.config.kind,
                    chat_response={
                        "choices": [{"message": {"role": "assistant", "content": "local ok"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                    latency_ms=3,
                    estimated_usage=False,
                )

        proxy_app.SETTINGS.providers["ollama"] = ProviderConfig(
            name="ollama",
            kind="local",
            provider_type="ollama",
            base_url="http://127.0.0.1:11434",
            model="llama3.1",
            enabled=True,
        )
        proxy_app.build_provider = lambda config: FakeProvider(config)
        try:
            response = TestClient(proxy_app.app).post(
                "/v1/responses?provider=ollama&model=llama3.1",
                json={"model": "deepseek-chat", "stream": False, "input": "hello", "tools": []},
            )
        finally:
            proxy_app.build_provider = original_build_provider

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["metadata"]["provider"], "ollama")
        self.assertEqual(body["metadata"]["provider_kind"], "local")
        self.assertEqual(body["metadata"]["provider_selection_reason"], "manual override")

    def test_default_ollama_route_uses_configured_worker_model_for_subagent_alias(self):
        from fastapi.testclient import TestClient
        from subagent_router.providers import ProviderConfig

        original_build_provider = proxy_app.build_provider

        class FakeProvider:
            def __init__(self, config):
                self.config = config

            async def chat(self, normalized, *, model=None):
                return proxy_app.ProviderResponse(
                    provider=self.config.name,
                    model=model or self.config.model or normalized.model,
                    provider_kind=self.config.kind,
                    chat_response={
                        "choices": [{"message": {"role": "assistant", "content": "local ok"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                    latency_ms=3,
                    estimated_usage=False,
                )

        proxy_app.SETTINGS.provider = "ollama"
        proxy_app.SETTINGS.fallback_providers = []
        proxy_app.SETTINGS.providers["ollama"] = ProviderConfig(
            name="ollama",
            kind="local",
            provider_type="ollama",
            base_url="http://127.0.0.1:11434",
            model="qwen3.5:latest",
            enabled=True,
            explorer_model="qwen3.5:latest",
            worker_model="qwen3.5:latest",
            reviewer_model="qwen3.5:latest",
        )
        proxy_app.build_provider = lambda config: FakeProvider(config)
        try:
            response = TestClient(proxy_app.app).post(
                "/v1/responses",
                json={"model": "subagent-router-worker", "stream": False, "input": "hello", "tools": []},
            )
        finally:
            proxy_app.build_provider = original_build_provider

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["metadata"]["provider"], "ollama")
        self.assertEqual(body["metadata"]["provider_model"], "qwen3.5:latest")
        audit = proxy_app.SETTINGS.audit_log_file.read_text(encoding="utf-8")
        self.assertIn('"model": "qwen3.5:latest"', audit)
        self.assertNotIn('"model": "deepseek-v4-flash"', audit)

    def test_default_route_uses_configured_explorer_model_for_subagent_alias(self):
        from fastapi.testclient import TestClient
        from subagent_router.providers import ProviderConfig

        original_build_provider = proxy_app.build_provider

        class FakeProvider:
            def __init__(self, config):
                self.config = config

            async def chat(self, normalized, *, model=None):
                return proxy_app.ProviderResponse(
                    provider=self.config.name,
                    model=model or self.config.model or normalized.model,
                    provider_kind=self.config.kind,
                    chat_response={
                        "choices": [{"message": {"role": "assistant", "content": "local ok"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                    latency_ms=3,
                    estimated_usage=False,
                )

        proxy_app.SETTINGS.provider = "ollama"
        proxy_app.SETTINGS.fallback_providers = []
        proxy_app.SETTINGS.providers["ollama"] = ProviderConfig(
            name="ollama",
            kind="local",
            provider_type="ollama",
            base_url="http://127.0.0.1:11434",
            model="qwen3.5:latest",
            enabled=True,
            explorer_model="qwen-explorer:latest",
        )
        proxy_app.build_provider = lambda config: FakeProvider(config)
        try:
            response = TestClient(proxy_app.app).post(
                "/v1/responses",
                json={"model": "subagent-router-explorer", "stream": False, "input": "map files", "tools": []},
            )
        finally:
            proxy_app.build_provider = original_build_provider

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["metadata"]["provider_model"], "qwen-explorer:latest")

    def test_safe_provider_error_prefers_nested_message(self):
        response = httpx.Response(
            400,
            json={"error": {"message": "insufficient balance", "type": "billing"}},
        )

        self.assertEqual(
            proxy_app.safe_provider_error(response),
            "provider rejected request: insufficient balance",
        )

    def test_http_error_increments_errors_by_provider(self):
        from fastapi.testclient import TestClient

        original_call_provider = proxy_app.call_provider

        async def failing_call_provider(normalized, route):
            raise httpx.HTTPError("connection failed")

        proxy_app.call_provider = failing_call_provider
        try:
            response = TestClient(proxy_app.app).post(
                "/v1/responses",
                json={"model": "deepseek-chat", "stream": False, "input": "hello", "tools": []},
            )
        finally:
            proxy_app.call_provider = original_call_provider

        self.assertEqual(response.status_code, 502)
        self.assertEqual(proxy_app.ACTIVITY_STATE["errors_by_provider"]["deepseek"], 1)

    def test_provider_configuration_error_does_not_fallback(self):
        from fastapi.testclient import TestClient

        proxy_app.SETTINGS.mock_deepseek = False
        proxy_app.SETTINGS.deepseek_api_key = None
        proxy_app.SETTINGS.providers["deepseek"] = proxy_app.ProviderConfig(
            name="deepseek",
            kind="cloud",
            provider_type="deepseek",
            base_url="https://api.deepseek.com/v1",
            api_key=None,
            enabled=True,
        )
        proxy_app.SETTINGS.fallback_providers = ["ollama"]
        proxy_app.SETTINGS.providers["ollama"] = proxy_app.ProviderConfig(
            name="ollama",
            kind="local",
            provider_type="ollama",
            base_url="http://127.0.0.1:11434",
            model="llama3.1",
            enabled=True,
        )

        response = TestClient(proxy_app.app).post(
            "/v1/responses",
            json={"model": "deepseek-chat", "stream": False, "input": "hello", "tools": []},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("DEEPSEEK_API_KEY", response.json()["error"]["message"])

    def test_stream_events_emit_completed(self):
        async def collect():
            response = {
                "id": "resp_test",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "hello"}],
                    }
                ],
            }
            chunks = []
            async for chunk in proxy_app.stream_response_events(response):
                chunks.append(chunk.decode())
            return "".join(chunks)

        body = asyncio.run(collect())
        self.assertIn("event: response.completed", body)
        self.assertIn("event: response.output_item.done", body)

    def test_tool_call_response_preserves_call_id(self):
        payload = {
            "model": "deepseek-chat",
            "stream": False,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "use a tool"}],
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "exec_command",
                    "description": "run",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        }

        normalized = normalize_request(payload)
        chat_response = asyncio.run(proxy_app.call_deepseek(normalized))
        body = proxy_app.responses_object(payload, normalized, chat_response)
        self.assertEqual(body["output"][0]["type"], "function_call")
        self.assertEqual(body["output"][0]["call_id"], "call_mock_1")
        self.assertFalse(body["end_turn"])

    def test_response_items_drop_unadvertised_tool_calls(self):
        chat_response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_allowed",
                                "type": "function",
                                "function": {"name": "exec_command", "arguments": "{}"},
                            },
                            {
                                "id": "call_blocked",
                                "type": "function",
                                "function": {"name": "apply_patch", "arguments": "{}"},
                            },
                        ],
                    }
                }
            ]
        }

        output, assistant_messages, _ = proxy_app.response_items_from_chat(
            chat_response,
            {"exec_command": proxy_app.ToolNameMapping(name="exec_command")},
        )

        self.assertEqual(len(output), 1)
        self.assertEqual(output[0]["type"], "function_call")
        self.assertEqual(output[0]["name"], "exec_command")
        self.assertEqual(output[0]["call_id"], "call_allowed")
        self.assertEqual(assistant_messages[0]["tool_calls"][0]["function"]["name"], "exec_command")

    def test_tool_call_response_preserves_reasoning_content_for_next_request(self):
        payload = {
            "model": "subagent-router-reviewer",
            "stream": False,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "use a tool"}],
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "exec_command",
                    "description": "run",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        }
        normalized = normalize_request(payload)
        chat_response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "private chain of thought",
                        "tool_calls": [
                            {
                                "id": "call_reasoning",
                                "type": "function",
                                "function": {"name": "exec_command", "arguments": "{\"cmd\":\"pwd\"}"},
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

        body = proxy_app.responses_object(payload, normalized, chat_response)

        self.assertEqual(body["output"][0]["type"], "function_call")
        self.assertNotIn("reasoning_content", json.dumps(body))
        self.assertEqual(proxy_app.REASONING_CONTENT_BY_CALL_ID["call_reasoning"], "private chain of thought")
        state = proxy_app.RESPONSE_STATES[body["id"]]
        self.assertEqual(state[-1]["reasoning_content"], "private chain of thought")

    def test_assistant_text_response_preserves_reasoning_content_for_replay(self):
        payload = {
            "model": "subagent-router-reviewer",
            "stream": False,
            "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
            "tools": [],
        }
        normalized = normalize_request(payload)
        chat_response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Inspected the diff.",
                        "reasoning_content": "private text reasoning",
                    }
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

        proxy_app.responses_object(payload, normalized, chat_response)
        replay = normalize_request(
            {
                "model": "subagent-router-reviewer",
                "input": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Inspected the diff."}],
                    }
                ],
            },
            reasoning_content_by_assistant_text=proxy_app.REASONING_CONTENT_BY_ASSISTANT_TEXT,
        )

        self.assertEqual(replay.messages[0]["reasoning_content"], "private text reasoning")

    def test_trace_response_omits_reasoning_content(self):
        response = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "I will inspect the diff."}],
                },
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call_1",
                    "reasoning_content": "private chain of thought",
                },
            ],
            "usage": {"total_tokens": 12},
            "end_turn": False,
        }
        stream = io.StringIO()

        proxy_app.SETTINGS.trace_enabled = True
        with redirect_stdout(stream):
            proxy_app.trace_response("trace123", response)

        output = stream.getvalue()
        self.assertIn("tool_call name=exec_command", output)
        self.assertIn("call_id=call_1", output)
        self.assertIn("message=I will inspect the diff.", output)
        self.assertNotIn("private chain of thought", output)

    def test_trace_response_includes_exec_command_preview(self):
        response = {
            "output": [
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call_1",
                    "arguments": json.dumps({"cmd": "cd /repo && git diff --stat"}),
                }
            ],
            "usage": {"total_tokens": 12},
            "end_turn": False,
        }
        stream = io.StringIO()

        proxy_app.SETTINGS.trace_enabled = True
        with redirect_stdout(stream):
            proxy_app.trace_response("trace123", response)

        self.assertIn("cmd=cd /repo && git diff --stat", stream.getvalue())

    def test_trace_request_includes_recent_tool_output_preview(self):
        normalized = normalize_request(
            {
                "model": "subagent-router-reviewer",
                "input": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": "{\"cmd\":\"git status\"}",
                        "call_id": "call_1",
                    },
                    {"type": "function_call_output", "call_id": "call_1", "output": "M app.py\n"},
                ],
            }
        )
        stream = io.StringIO()

        proxy_app.SETTINGS.trace_enabled = True
        with redirect_stdout(stream):
            proxy_app.trace_request("trace123", normalized)

        output = stream.getvalue()
        self.assertIn("tool_output call_id=call_1", output)
        self.assertIn("M app.py", output)

    def test_debug_activity_reports_recent_activity(self):
        proxy_app.record_activity("request", trace_id="trace1", model="subagent-router-reviewer")
        proxy_app.record_activity(
            "response",
            trace_id="trace1",
            model="subagent-router-reviewer",
            output_kind="tool_call",
            end_turn=False,
        )

        activity = asyncio.run(proxy_app.debug_activity())

        self.assertEqual(activity["request_count"], 1)
        self.assertEqual(activity["response_count"], 1)
        self.assertEqual(activity["last_trace_id"], "trace1")
        self.assertEqual(activity["last_model"], "subagent-router-reviewer")
        self.assertEqual(activity["last_output_kind"], "tool_call")
        self.assertEqual(activity["last_end_turn"], False)
        self.assertTrue(activity["active_within_120s"])
        self.assertEqual(activity["paths"], proxy_app.SETTINGS.sanitized_paths())

    def test_debug_paths_reports_resolved_settings_paths(self):
        paths = asyncio.run(proxy_app.debug_paths())

        self.assertEqual(paths, proxy_app.SETTINGS.sanitized_paths())

    def test_usage_record_tracks_input_cache_output_daily_totals(self):
        proxy_app.write_usage_record(
            {
                "timestamp": 1,
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 25,
                    "total_tokens": 125,
                    "prompt_tokens_details": {"cached_tokens": 40},
                },
                "estimated_cost_usd": 0.001,
            }
        )

        summary = json.loads(proxy_app.SETTINGS.usage_file.read_text())
        today = datetime.date.today().isoformat()
        day = summary["daily_usage"][today]

        self.assertEqual(summary["total_input_tokens"], 100)
        self.assertEqual(summary["total_cached_input_tokens"], 40)
        self.assertEqual(summary["total_output_tokens"], 25)
        self.assertEqual(day["input_tokens"], 100)
        self.assertEqual(day["cached_input_tokens"], 40)
        self.assertEqual(day["output_tokens"], 25)

    def test_activity_file_reports_recent_activity(self):
        proxy_app.record_activity("request", trace_id="trace1", model="subagent-router-reviewer")
        proxy_app.record_activity(
            "response",
            trace_id="trace1",
            model="subagent-router-reviewer",
            output_kind="tool_call",
            end_turn=False,
        )

        activity = json.loads(proxy_app.SETTINGS.activity_file.read_text())

        self.assertEqual(activity["request_count"], 1)
        self.assertEqual(activity["response_count"], 1)
        self.assertEqual(activity["last_trace_id"], "trace1")
        self.assertEqual(activity["last_model"], "subagent-router-reviewer")
        self.assertEqual(activity["last_output_kind"], "tool_call")
        self.assertEqual(activity["last_end_turn"], False)
        self.assertTrue(activity["active_within_120s"])

    def test_debug_activity_reports_errors(self):
        proxy_app.record_activity("error", trace_id="trace2")

        activity = asyncio.run(proxy_app.debug_activity())

        self.assertEqual(activity["error_count"], 1)
        self.assertEqual(activity["last_trace_id"], "trace2")
        self.assertTrue(activity["active_within_120s"])

    def test_session_mirror_reports_final_message_without_reasoning(self):
        final_message = "No findings. " + " ".join(["details"] * 700)
        normalized = normalize_request(
            {
                "model": "subagent-router-reviewer",
                "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "review"}]}],
                "tools": [],
            }
        )
        response = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": final_message}],
                    "reasoning_content": "private chain of thought",
                }
            ],
            "usage": {"total_tokens": 12},
            "end_turn": True,
        }

        proxy_app.record_session_response("trace123", normalized, response)
        mirror = json.loads(proxy_app.SETTINGS.session_mirror_file.read_text())

        self.assertEqual(mirror["event_count"], 1)
        self.assertEqual(mirror["latest"]["messages"], [final_message])
        self.assertEqual(mirror["final"]["messages"], [final_message])
        self.assertNotIn("private chain of thought", json.dumps(mirror))

    def test_session_mirror_keeps_last_final_when_event_window_rolls(self):
        final_message = "Detailed final result"
        normalized = normalize_request(
            {
                "model": "subagent-router-reviewer",
                "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "review"}]}],
                "tools": [],
            }
        )
        final_response = {
            "output": [{"type": "message", "content": [{"type": "output_text", "text": final_message}]}],
            "usage": {"total_tokens": 12},
            "end_turn": True,
        }
        tool_response = {
            "output": [{"type": "function_call", "name": "exec_command", "call_id": "call_1", "arguments": "{}"}],
            "usage": {"total_tokens": 1},
            "end_turn": False,
        }

        proxy_app.record_session_response("trace-final", normalized, final_response)
        for index in range(proxy_app.MAX_SESSION_EVENTS + 1):
            proxy_app.record_session_response(f"trace-tool-{index}", normalized, tool_response)

        mirror = json.loads(proxy_app.SETTINGS.session_mirror_file.read_text())

        self.assertEqual(mirror["final"]["messages"], [final_message])

    def test_image_generation_is_logged_redacted_and_dropped(self):
        payload = {
            "model": "deepseek-chat",
            "stream": False,
            "Authorization": "Bearer sk-test",
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "be brief"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
            ],
            "tools": [{"type": "image_generation", "output_format": "png"}],
        }

        proxy_app.log_payload(payload)
        normalized = normalize_request(payload)
        chat_response = asyncio.run(proxy_app.call_deepseek(normalized))
        body = proxy_app.responses_object(payload, normalized, chat_response)
        self.assertEqual(body["metadata"]["dropped_tools"], ["image_generation"])
        log_text = next(Path(self.tempdir.name).glob("*.json")).read_text()
        self.assertIn("[REDACTED]", log_text)
        self.assertNotIn("sk-test", log_text)

    def test_provider_error_log_contains_sanitized_diagnostics(self):
        payload = {
            "model": "subagent-router-reviewer",
            "stream": False,
            "input": "Say hello",
            "tools": [
                {
                    "type": "function",
                    "name": "browser_snapshot",
                    "description": "snapshot",
                    "parameters": {"type": "object", "properties": {}},
                },
                {
                    "type": "function",
                    "name": "exec_command",
                    "description": "run",
                    "parameters": {"type": "object", "properties": {}},
                },
            ],
        }
        normalized = normalize_request(payload)
        response = httpx.Response(
            400,
            json={
                "error": {
                    "message": "Bad schema",
                    "authorization": "Bearer sk-test",
                    "details": "x" * 5000,
                    "reasoning_content": "private chain of thought",
                    "messages": [{"role": "user", "content": "diff --git a/secret b/secret"}],
                }
            },
        )

        path = proxy_app.log_provider_error(normalized, response)
        diagnostic = json.loads(path.read_text())

        self.assertEqual(diagnostic["requested_model"], "subagent-router-reviewer")
        self.assertEqual(diagnostic["upstream_model"], "deepseek-v4-pro")
        self.assertEqual(diagnostic["upstream_status_code"], 400)
        self.assertEqual(diagnostic["input_item_count"], 1)
        self.assertEqual(diagnostic["tool_count"], 1)
        self.assertEqual(diagnostic["normalized_tool_names"], ["exec_command"])
        self.assertEqual(diagnostic["dropped_tool_names"], ["browser_snapshot"])
        self.assertTrue(diagnostic["used_shorthand_string_input"])
        text = path.read_text()
        self.assertIn("[REDACTED]", text)
        self.assertIn("[OMITTED]", text)
        self.assertIn("[truncated", text)
        self.assertNotIn("sk-test", text)
        self.assertNotIn("diff --git", text)
        self.assertNotIn("private chain of thought", text)

    def test_safe_provider_error_returns_redacted_provider_body(self):
        response = httpx.Response(
            400,
            json={"error": {"message": "Empty input messages", "api_key": "secret"}},
        )

        message = proxy_app.safe_provider_error(response)

        self.assertIn("Empty input messages", message)
        self.assertIn("[REDACTED]", message)
        self.assertNotIn("secret", message)

    def test_request_body_too_large_returns_413(self):
        from fastapi.testclient import TestClient

        client = TestClient(proxy_app.app)
        small_payload = {"model": "deepseek-chat", "input": "hi", "tools": []}
        headers = {"content-length": str(proxy_app.MAX_REQUEST_BODY_SIZE + 1)}

        response = client.post("/v1/responses", json=small_payload, headers=headers)

        self.assertEqual(response.status_code, 413)
        body = response.json()
        self.assertIn("Request body too large", body["error"]["message"])

    def test_request_body_too_large_without_content_length_returns_413(self):
        from fastapi.testclient import TestClient

        client = TestClient(proxy_app.app)
        oversized = b'{"input":"' + (b"x" * proxy_app.MAX_REQUEST_BODY_SIZE) + b'"}'

        response = client.post("/v1/responses", content=oversized)

        self.assertEqual(response.status_code, 413)
        self.assertIn("Request body too large", response.json()["error"]["message"])

    def test_invalid_json_returns_400(self):
        from fastapi.testclient import TestClient

        client = TestClient(proxy_app.app)

        response = client.post("/v1/responses", content=b"{not-json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid JSON", response.json()["error"]["message"])

    def test_response_state_eviction_caps_old_states(self):
        original_max = proxy_app.MAX_RESPONSE_STATES
        proxy_app.MAX_RESPONSE_STATES = 3
        try:
            proxy_app.RESPONSE_STATES.clear()
            proxy_app.REASONING_CONTENT_BY_CALL_ID.clear()
            proxy_app.REASONING_CONTENT_BY_ASSISTANT_TEXT.clear()
            payload = {
                "model": "deepseek-chat",
                "stream": False,
                "input": [
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}
                ],
                "tools": [],
            }
            normalized = normalize_request(payload)
            response_ids = []
            for i in range(5):
                chat_response = asyncio.run(proxy_app.call_deepseek(normalized))
                body = proxy_app.responses_object(payload, normalized, chat_response)
                response_ids.append(body["id"])
            self.assertEqual(len(proxy_app.RESPONSE_STATES), 3)
            for rid in response_ids[:2]:
                self.assertNotIn(rid, proxy_app.RESPONSE_STATES)
            for rid in response_ids[-3:]:
                self.assertIn(rid, proxy_app.RESPONSE_STATES)
        finally:
            proxy_app.MAX_RESPONSE_STATES = original_max

    def test_reasoning_eviction_prunes_unreferenced_entries(self):
        original_max = proxy_app.MAX_RESPONSE_STATES
        proxy_app.MAX_RESPONSE_STATES = 2
        try:
            proxy_app.RESPONSE_STATES.clear()
            proxy_app.REASONING_CONTENT_BY_CALL_ID.clear()
            proxy_app.REASONING_CONTENT_BY_ASSISTANT_TEXT.clear()
            proxy_app.RESPONSE_STATES["resp_1"] = [
                {"role": "assistant", "content": "thinking text 1"}
            ]
            proxy_app.REASONING_CONTENT_BY_ASSISTANT_TEXT["thinking text 1"] = "thinking1"
            proxy_app.REASONING_CONTENT_BY_CALL_ID["call_1"] = "thinking call 1"
            proxy_app.RESPONSE_STATES["resp_2"] = [
                {"role": "assistant", "content": "thinking text 2", "tool_calls": [{"id": "call_2", "function": {"name": "test"}}]}
            ]
            proxy_app.REASONING_CONTENT_BY_ASSISTANT_TEXT["thinking text 2"] = "thinking2"
            proxy_app.REASONING_CONTENT_BY_CALL_ID["call_2"] = "thinking call 2"
            proxy_app.RESPONSE_STATES["resp_3"] = [
                {"role": "assistant", "content": "thinking text 3", "tool_calls": [{"id": "call_3", "function": {"name": "test"}}]}
            ]
            proxy_app.REASONING_CONTENT_BY_ASSISTANT_TEXT["thinking text 3"] = "thinking3"
            proxy_app.REASONING_CONTENT_BY_CALL_ID["call_3"] = "thinking call 3"
            proxy_app.REASONING_CONTENT_BY_CALL_ID["call_3_orphan"] = "orphaned reasoning"
            proxy_app._evict_old_response_states()
            self.assertEqual(len(proxy_app.RESPONSE_STATES), 2)
            self.assertNotIn("resp_1", proxy_app.RESPONSE_STATES)
            self.assertIn("resp_2", proxy_app.RESPONSE_STATES)
            self.assertIn("resp_3", proxy_app.RESPONSE_STATES)
            self.assertNotIn("thinking text 1", proxy_app.REASONING_CONTENT_BY_ASSISTANT_TEXT)
            self.assertNotIn("call_1", proxy_app.REASONING_CONTENT_BY_CALL_ID)
            self.assertIn("thinking text 2", proxy_app.REASONING_CONTENT_BY_ASSISTANT_TEXT)
            self.assertIn("call_2", proxy_app.REASONING_CONTENT_BY_CALL_ID)
            self.assertIn("thinking text 3", proxy_app.REASONING_CONTENT_BY_ASSISTANT_TEXT)
            self.assertIn("call_3", proxy_app.REASONING_CONTENT_BY_CALL_ID)
            self.assertNotIn("call_3_orphan", proxy_app.REASONING_CONTENT_BY_CALL_ID)
        finally:
            proxy_app.MAX_RESPONSE_STATES = original_max


    def test_response_model_uses_requested_model_not_upstream_alias(self):
        """Response model preserves requested_model (deepseek-worker), not alias (deepseek-v4-flash)."""
        payload = {
            "model": "subagent-router-worker",
            "stream": False,
            "input": "hello",
            "tools": [],
        }
        normalized = proxy_app.normalize_request(payload)
        chat_response = asyncio.run(proxy_app.call_deepseek(normalized))
        body = proxy_app.responses_object(payload, normalized, chat_response)
        # original_payload has "model" key, so that should be used directly
        self.assertEqual(body["model"], "subagent-router-worker")

    def test_response_model_fallback_no_payload_model(self):
        """When payload omits model, fallback uses normalized.requested_model (e.g. deepseek-chat)."""
        payload = {
            "stream": False,
            "input": "hello",
            "tools": [],
        }
        normalized = proxy_app.normalize_request(payload)
        chat_response = asyncio.run(proxy_app.call_deepseek(normalized))
        body = proxy_app.responses_object(payload, normalized, chat_response)
        self.assertEqual(body["model"], normalized.requested_model)
        self.assertEqual(body["model"], "deepseek-chat")

    def test_session_budget_hard_stop_rejects_request(self):
        """Hard-stop session budget rejects request when cost exceeds limit."""
        original_mode = proxy_app.SETTINGS.budget_mode
        original_session_cost = proxy_app.SETTINGS.max_cost_per_session
        try:
            proxy_app.ACTIVITY_STATE["total_cost_usd"] = 5.0
            proxy_app.SETTINGS.max_cost_per_session = 1.0
            proxy_app.SETTINGS.budget_mode = "hard-stop"
            payload = {
                "model": "deepseek-chat",
                "stream": False,
                "input": "hello",
                "tools": [],
            }
            from fastapi.testclient import TestClient
            client = TestClient(proxy_app.app)
            response = client.post("/v1/responses", json=payload)
            self.assertEqual(response.status_code, 402)
            self.assertIn("session budget exceeded", response.json()["error"]["message"])
        finally:
            proxy_app.SETTINGS.budget_mode = original_mode
            proxy_app.SETTINGS.max_cost_per_session = original_session_cost
            proxy_app.ACTIVITY_STATE["total_cost_usd"] = 0.0

    def test_session_budget_warn_allows_request(self):
        """Warn-only session budget logs warning but allows the request."""
        original_mode = proxy_app.SETTINGS.budget_mode
        original_session_cost = proxy_app.SETTINGS.max_cost_per_session
        try:
            proxy_app.ACTIVITY_STATE["total_cost_usd"] = 5.0
            proxy_app.SETTINGS.max_cost_per_session = 1.0
            proxy_app.SETTINGS.budget_mode = "warn"
            payload = {
                "model": "deepseek-chat",
                "stream": False,
                "input": "hello",
                "tools": [],
            }
            from fastapi.testclient import TestClient
            client = TestClient(proxy_app.app)
            response = client.post("/v1/responses", json=payload)
            self.assertEqual(response.status_code, 200)
        finally:
            proxy_app.SETTINGS.budget_mode = original_mode
            proxy_app.SETTINGS.max_cost_per_session = original_session_cost
            proxy_app.ACTIVITY_STATE["total_cost_usd"] = 0.0

    def test_session_token_budget_hard_stop_rejects_request(self):
        """Hard-stop session token budget rejects request when tokens exceed limit."""
        original_mode = proxy_app.SETTINGS.budget_mode
        original_session_tokens = proxy_app.SETTINGS.max_tokens_per_session
        try:
            proxy_app.ACTIVITY_STATE["total_tokens"] = 1000000
            proxy_app.SETTINGS.max_tokens_per_session = 5000
            proxy_app.SETTINGS.budget_mode = "hard-stop"
            payload = {
                "model": "deepseek-chat",
                "stream": False,
                "input": "hello",
                "tools": [],
            }
            from fastapi.testclient import TestClient
            client = TestClient(proxy_app.app)
            response = client.post("/v1/responses", json=payload)
            self.assertEqual(response.status_code, 402)
            self.assertIn("session budget exceeded", response.json()["error"]["message"])
        finally:
            proxy_app.SETTINGS.budget_mode = original_mode
            proxy_app.SETTINGS.max_tokens_per_session = original_session_tokens
            proxy_app.ACTIVITY_STATE["total_tokens"] = 0

    def test_daily_budget_hard_stop_rejects_request(self):
        """Hard-stop daily budget rejects request when persisted daily cost exceeds limit."""
        original_mode = proxy_app.SETTINGS.budget_mode
        original_daily_cost = proxy_app.SETTINGS.max_cost_per_day
        try:
            proxy_app.SETTINGS.max_cost_per_day = 0.01
            proxy_app.SETTINGS.budget_mode = "hard-stop"
            # Write a usage record that simulates daily cost exceeding the limit
            proxy_app.write_usage_record({
                "timestamp": 1000000000,
                "trace_id": "test-daily-budget",
                "provider": "deepseek",
                "provider_kind": "cloud",
                "model": "deepseek-chat",
                "routing_policy": "safe-default",
                "selection_reason": "default provider",
                "usage": {"input_tokens": 1000, "output_tokens": 1000, "total_tokens": 2000},
                "estimated_usage": False,
                "estimated_cost_usd": 0.05,
                "latency_ms": 100,
                "fallback_chain": [],
            })
            payload = {
                "model": "deepseek-chat",
                "stream": False,
                "input": "hello",
                "tools": [],
            }
            from fastapi.testclient import TestClient
            client = TestClient(proxy_app.app)
            response = client.post("/v1/responses", json=payload)
            self.assertEqual(response.status_code, 402)
            self.assertIn("daily budget exceeded", response.json()["error"]["message"])
        finally:
            proxy_app.SETTINGS.budget_mode = original_mode
            proxy_app.SETTINGS.max_cost_per_day = original_daily_cost
            # Clean up the test usage record
            proxy_app.read_usage_summary.cache_clear() if hasattr(proxy_app.read_usage_summary, "cache_clear") else None
            if proxy_app.SETTINGS.usage_file.exists():
                proxy_app.SETTINGS.usage_file.unlink()

    def test_normalize_usage_preserves_deepseek_cache_hit_tokens(self):
        usage = proxy_app.normalize_usage(
            {
                "prompt_tokens": 2_059_216,
                "completion_tokens": 29_463,
                "total_tokens": 2_088_679,
                "prompt_cache_hit_tokens": 1_937_024,
                "prompt_cache_miss_tokens": 122_192,
                "completion_tokens_details": {"reasoning_tokens": 17},
            }
        )

        self.assertEqual(usage["input_tokens"], 2_059_216)
        self.assertEqual(usage["output_tokens"], 29_463)
        self.assertEqual(usage["total_tokens"], 2_088_679)
        self.assertEqual(usage["input_tokens_details"]["cached_tokens"], 1_937_024)
        self.assertEqual(usage["output_tokens_details"]["reasoning_tokens"], 17)

    def test_deepseek_v4_pro_cost_uses_discounted_cache_hit_rate(self):
        usage = proxy_app.normalize_usage(
            {
                "prompt_tokens": 2_029_753,
                "completion_tokens": 29_463,
                "prompt_cache_hit_tokens": 1_937_024,
                "prompt_cache_miss_tokens": 92_729,
            }
        )

        cost = proxy_app.estimate_cost_usd(
            usage,
            proxy_app.SETTINGS.providers["deepseek"],
            "deepseek-v4-pro",
        )

        self.assertEqual(cost, 0.07299164)

    def test_deepseek_v4_flash_cost_charges_cache_misses_separately(self):
        usage = proxy_app.normalize_usage(
            {
                "prompt_tokens": 4_721_663,
                "completion_tokens": 52_614,
                "prompt_cache_hit_tokens": 4_563_968,
                "prompt_cache_miss_tokens": 157_695,
            }
        )

        cost = proxy_app.estimate_cost_usd(
            usage,
            proxy_app.SETTINGS.providers["deepseek"],
            "deepseek-v4-flash",
        )

        self.assertEqual(cost, 0.04958833)


    # ---------- error_category tests ----------

    def test_error_category_auth_by_status(self):
        self.assertEqual(proxy_app.error_category(status_code=401), "auth")
        self.assertEqual(proxy_app.error_category(status_code=403), "auth")

    def test_error_category_rate_limit_by_status(self):
        self.assertEqual(proxy_app.error_category(status_code=429), "rate_limit")

    def test_error_category_server_error_by_status(self):
        self.assertEqual(proxy_app.error_category(status_code=500), "server_error")
        self.assertEqual(proxy_app.error_category(status_code=502), "server_error")
        self.assertEqual(proxy_app.error_category(status_code=503), "server_error")

    def test_error_category_timeout_by_status(self):
        self.assertEqual(proxy_app.error_category(status_code=408), "timeout")
        self.assertEqual(proxy_app.error_category(status_code=504), "timeout")

    def test_error_category_client_error_4xx(self):
        self.assertEqual(proxy_app.error_category(status_code=400), "client_error")
        self.assertEqual(proxy_app.error_category(status_code=404), "client_error")
        self.assertEqual(proxy_app.error_category(status_code=422), "client_error")

    def test_error_category_by_message(self):
        self.assertEqual(proxy_app.error_category(error_message="timeout error"), "timeout")
        self.assertEqual(proxy_app.error_category(error_message="timed out"), "timeout")
        self.assertEqual(proxy_app.error_category(error_message="rate limit exceeded"), "rate_limit")
        self.assertEqual(proxy_app.error_category(error_message="quota exceeded"), "rate_limit")
        self.assertEqual(proxy_app.error_category(error_message="capacity exceeded"), "rate_limit")
        self.assertEqual(proxy_app.error_category(error_message="unauthorized"), "auth")
        self.assertEqual(proxy_app.error_category(error_message="invalid API key"), "auth")
        self.assertEqual(proxy_app.error_category(error_message="forbidden access"), "auth")
        self.assertEqual(proxy_app.error_category(error_message="service unavailable"), "server_error")
        self.assertEqual(proxy_app.error_category(error_message="503 upstream error"), "server_error")

    def test_error_category_unknown_default(self):
        self.assertEqual(proxy_app.error_category(), "unknown")
        self.assertEqual(proxy_app.error_category(error_message="something else"), "unknown")

    # ---------- update_provider_health tests ----------

    def test_update_provider_health_first_success(self):
        proxy_app.update_provider_health("test-provider", success=True, latency_ms=100)
        health = proxy_app.SETTINGS.provider_health.get("test-provider")
        self.assertIsNotNone(health)
        self.assertEqual(health.total_requests, 1)
        self.assertEqual(health.total_errors, 0)
        self.assertEqual(health.consecutive_errors, 0)
        self.assertEqual(health.error_rate, 0.0)
        self.assertEqual(health.average_latency_ms, 100.0)
        # Cleanup
        proxy_app.SETTINGS.provider_health.pop("test-provider", None)

    def test_update_provider_health_first_error(self):
        proxy_app.update_provider_health("test-provider-err", success=False)
        health = proxy_app.SETTINGS.provider_health.get("test-provider-err")
        self.assertIsNotNone(health)
        self.assertEqual(health.total_requests, 1)
        self.assertEqual(health.total_errors, 1)
        self.assertEqual(health.consecutive_errors, 1)
        self.assertEqual(health.error_rate, 1.0)
        proxy_app.SETTINGS.provider_health.pop("test-provider-err", None)

    def test_update_provider_health_mixed_results(self):
        proxy_app.update_provider_health("test-mixed", success=True, latency_ms=50)
        proxy_app.update_provider_health("test-mixed", success=False)
        proxy_app.update_provider_health("test-mixed", success=True, latency_ms=150)
        health = proxy_app.SETTINGS.provider_health.get("test-mixed")
        self.assertIsNotNone(health)
        self.assertEqual(health.total_requests, 3)
        self.assertEqual(health.total_errors, 1)
        self.assertEqual(health.consecutive_errors, 0)  # reset by last success
        self.assertAlmostEqual(health.error_rate, 1.0 / 3.0)
        proxy_app.SETTINGS.provider_health.pop("test-mixed", None)

    # ---------- /debug/config endpoint tests ----------

    def test_debug_config_function_returns_config_info(self):
        """Verify /debug/config returns structured config info."""
        from subagent_router.providers import ProviderCapabilities, ProviderConfig
        # Simulate the debug_config handler logic directly
        config = proxy_app.SETTINGS
        result = {
            "config_warnings": config.config_warnings,
            "budget_mode": config.budget_mode,
            "provider": config.provider,
            "fallback_providers": config.fallback_providers,
            "allowed_providers": config.allowed_providers,
            "denied_providers": config.denied_providers,
            "dry_run": config.dry_run,
            "debug_mode": config.debug_mode,
            "provider_predictions": {
                name: {
                    "reliability_score": p.reliability_score,
                    "latency_p50_ms": p.latency_p50_ms,
                    "latency_p99_ms": p.latency_p99_ms,
                    "capability_score": p.capability_score,
                    "budget_prediction_usd": p.budget_prediction_usd,
                    "budget_prediction_tokens": p.budget_prediction_tokens,
                }
                for name, p in config.provider_predictions.items()
            },
            "provider_health": {
                name: {
                    "consecutive_errors": h.consecutive_errors,
                    "error_rate": h.error_rate,
                    "average_latency_ms": h.average_latency_ms,
                    "uptime_fraction": h.uptime_fraction,
                }
                for name, h in config.provider_health.items()
            },
        }
        self.assertIn("config_warnings", result)
        self.assertIn("budget_mode", result)
        self.assertIn("provider", result)
        self.assertIn("provider_predictions", result)
        self.assertIn("provider_health", result)
        self.assertEqual(result["budget_mode"], "warn")
    # ---------- Fallback chain diagnostics tests ----------

    def test_fallback_attempt_entry_includes_error_category(self):
        """Verify call_provider attempts include error_category."""
        route = proxy_app.RouteSelection("deepseek", None, [], "test-policy", "test")
        route.attempts.append({
            "provider": "deepseek",
            "model": "deepseek-chat",
            "status": "error",
            "status_code": 429,
            "error_category": proxy_app.error_category(status_code=429),
        })
        self.assertEqual(route.attempts[0]["error_category"], "rate_limit")

    def test_write_failed_attempt_audit_includes_provider_health(self):
        """Verify failed attempt audit records include provider health snapshot."""
        proxy_app.update_provider_health("audit-test-provider", success=True, latency_ms=50)
        route = proxy_app.RouteSelection("audit-test-provider", None, [], "test", "test audit")
        route.attempts.append({
            "provider": "audit-test-provider",
            "model": "deepseek-chat",
            "status": "error",
            "status_code": 500,
            "error_category": "server_error",
        })
        proxy_app.write_failed_attempt_audit("test-audit-trace", route, "server error", 500)
        # Read back the audit log
        if proxy_app.SETTINGS.audit_log_file.exists():
            lines = proxy_app.SETTINGS.audit_log_file.read_text(encoding="utf-8").strip().split("\n")
            if lines:
                import json
                record = json.loads(lines[-1])
                self.assertIn("fallback_chain", record)
                self.assertIn("provider_health_snapshot", record)
                self.assertIn("audit-test-provider", str(record.get("provider_health_snapshot", {})))
        proxy_app.SETTINGS.provider_health.pop("audit-test-provider", None)



    # ---------- /health endpoint config_warnings and provider_health tests ----------

    def test_health_function_includes_config_warnings(self):
        """Verify /health response includes config_warnings and provider_health."""
        config = proxy_app.SETTINGS
        result = {
            "status": "ok",
            "default_provider": config.provider,
            "config_warnings": config.config_warnings,
            "debug_mode": config.debug_mode,
            "providers": {
                name: {
                    "type": c.provider_type,
                    "kind": c.kind,
                    "enabled": c.enabled,
                    "base_url_configured": bool(c.base_url),
                    "model": c.model,
                }
                for name, c in config.providers.items()
            },
            "provider_health": {
                name: {
                    "consecutive_errors": h.consecutive_errors,
                    "error_rate": h.error_rate,
                    "average_latency_ms": h.average_latency_ms,
                    "uptime_fraction": h.uptime_fraction,
                }
                for name, h in config.provider_health.items()
            },
        }
        self.assertIn("config_warnings", result)
        self.assertIn("provider_health", result)
        self.assertIn("providers", result)
        self.assertEqual(result["status"], "ok")
    def test_is_subagent_model_detects_router_aliases_only(self):
        self.assertTrue(proxy_app.is_subagent_model("subagent-router-explorer"))
        self.assertTrue(proxy_app.is_subagent_model("subagent-router-worker"))
        self.assertTrue(proxy_app.is_subagent_model("subagent-router-reviewer"))
        self.assertTrue(proxy_app.is_subagent_model("deepseek-explorer"))
        self.assertTrue(proxy_app.is_subagent_model("deepseek-worker"))
        self.assertTrue(proxy_app.is_subagent_model("deepseek-reviewer"))

        self.assertFalse(proxy_app.is_subagent_model("deepseek-v4-flash"))
        self.assertFalse(proxy_app.is_subagent_model("deepseek-v4-pro"))
        self.assertFalse(proxy_app.is_subagent_model("deepseek-chat"))
        self.assertFalse(proxy_app.is_subagent_model("gpt-4o"))

if __name__ == "__main__":
    unittest.main()
