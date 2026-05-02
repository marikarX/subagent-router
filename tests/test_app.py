import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import httpx

import subagent_router.app as proxy_app
from subagent_router.normalization import normalize_request


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_settings = proxy_app.SETTINGS
        proxy_app.SETTINGS = proxy_app.Settings.from_env()
        proxy_app.SETTINGS.mock_deepseek = True
        proxy_app.SETTINGS.trace_enabled = False
        proxy_app.SETTINGS.log_dir = Path(self.tempdir.name)
        proxy_app.SETTINGS.provider_error_log_dir = Path(self.tempdir.name) / "provider_errors"
        proxy_app.SETTINGS.activity_file = Path(self.tempdir.name) / "activity.json"
        proxy_app.SETTINGS.session_mirror_file = Path(self.tempdir.name) / "session_mirror.json"
        proxy_app.RESPONSE_STATES.clear()
        proxy_app.REASONING_CONTENT_BY_CALL_ID.clear()
        proxy_app.REASONING_CONTENT_BY_ASSISTANT_TEXT.clear()
        proxy_app.SESSION_EVENTS.clear()
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
                "last_output_kind": None,
                "last_end_turn": None,
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
                "last_output_kind": None,
                "last_end_turn": None,
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

    def test_tool_call_response_preserves_reasoning_content_for_next_request(self):
        payload = {
            "model": "deepseek-reviewer",
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
            "model": "deepseek-reviewer",
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
                        "content": "I will inspect the diff.",
                        "reasoning_content": "private text reasoning",
                    }
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

        proxy_app.responses_object(payload, normalized, chat_response)
        replay = normalize_request(
            {
                "model": "deepseek-reviewer",
                "input": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "I will inspect the diff."}],
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
                "model": "deepseek-reviewer",
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
        proxy_app.record_activity("request", trace_id="trace1", model="deepseek-reviewer")
        proxy_app.record_activity(
            "response",
            trace_id="trace1",
            model="deepseek-reviewer",
            output_kind="tool_call",
            end_turn=False,
        )

        activity = asyncio.run(proxy_app.debug_activity())

        self.assertEqual(activity["request_count"], 1)
        self.assertEqual(activity["response_count"], 1)
        self.assertEqual(activity["last_trace_id"], "trace1")
        self.assertEqual(activity["last_model"], "deepseek-reviewer")
        self.assertEqual(activity["last_output_kind"], "tool_call")
        self.assertEqual(activity["last_end_turn"], False)
        self.assertTrue(activity["active_within_120s"])
        self.assertEqual(activity["paths"], proxy_app.SETTINGS.sanitized_paths())

    def test_debug_paths_reports_resolved_settings_paths(self):
        paths = asyncio.run(proxy_app.debug_paths())

        self.assertEqual(paths, proxy_app.SETTINGS.sanitized_paths())

    def test_activity_file_reports_recent_activity(self):
        proxy_app.record_activity("request", trace_id="trace1", model="deepseek-reviewer")
        proxy_app.record_activity(
            "response",
            trace_id="trace1",
            model="deepseek-reviewer",
            output_kind="tool_call",
            end_turn=False,
        )

        activity = json.loads(proxy_app.SETTINGS.activity_file.read_text())

        self.assertEqual(activity["request_count"], 1)
        self.assertEqual(activity["response_count"], 1)
        self.assertEqual(activity["last_trace_id"], "trace1")
        self.assertEqual(activity["last_model"], "deepseek-reviewer")
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
                "model": "deepseek-reviewer",
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
            "model": "deepseek-reviewer",
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

        self.assertEqual(diagnostic["requested_model"], "deepseek-reviewer")
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
            "model": "deepseek-worker",
            "stream": False,
            "input": "hello",
            "tools": [],
        }
        normalized = proxy_app.normalize_request(payload)
        chat_response = asyncio.run(proxy_app.call_deepseek(normalized))
        body = proxy_app.responses_object(payload, normalized, chat_response)
        # original_payload has "model" key, so that should be used directly
        self.assertEqual(body["model"], "deepseek-worker")

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


if __name__ == "__main__":
    unittest.main()
