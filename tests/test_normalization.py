import json
import unittest
from unittest.mock import patch

from subagent_router.normalization import (
    PayloadNormalizationError,
    normalize_request,
    redact,
)


class NormalizationTests(unittest.TestCase):
    def test_shorthand_input_becomes_user_message(self):
        normalized = normalize_request({"model": "deepseek-chat", "input": "Say hello", "tools": []})

        self.assertEqual(normalized.messages, [{"role": "user", "content": "Say hello"}])
        self.assertTrue(normalized.used_shorthand_input)
        self.assertEqual(normalized.input_item_count, 1)

    def test_model_aliases_map_to_upstream_model_names(self):
        self.assertEqual(normalize_request({"model": "deepseek-worker"}).model, "deepseek-v4-flash")
        self.assertEqual(normalize_request({"model": "deepseek-reviewer"}).model, "deepseek-v4-pro")
        self.assertEqual(normalize_request({"model": "deepseek-chat"}).model, "deepseek-chat")
        self.assertEqual(normalize_request({"model": "deepseek-v4-flash"}).model, "deepseek-v4-flash")
        self.assertEqual(normalize_request({"model": "deepseek-v4-pro"}).model, "deepseek-v4-pro")

    def test_developer_role_becomes_system(self):
        normalized = normalize_request(
            {
                "model": "deepseek-chat",
                "input": [
                    {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": "follow rules"}],
                    },
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                    },
                ],
                "tools": [],
            }
        )

        self.assertEqual(
            normalized.messages,
            [
                {"role": "system", "content": "follow rules"},
                {"role": "user", "content": "hello"},
            ],
        )

    def test_image_generation_tool_is_dropped(self):
        normalized = normalize_request(
            {
                "model": "deepseek-chat",
                "input": [],
                "tools": [
                    {"type": "image_generation", "output_format": "png"},
                    {
                        "type": "function",
                        "name": "exec_command",
                        "description": "run",
                        "parameters": {"type": "object", "properties": {}},
                    },
                ],
            }
        )

        self.assertEqual(normalized.dropped_tools, ["image_generation"])
        self.assertEqual(normalized.tools[0]["function"]["name"], "exec_command")

    def test_browser_tool_is_dropped_and_exec_command_is_preserved(self):
        normalized = normalize_request(
            {
                "model": "deepseek-reviewer",
                "input": "review",
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
        )

        self.assertEqual(normalized.dropped_tools, ["browser_snapshot"])
        self.assertEqual(normalized.normalized_tool_names, ["exec_command"])

    def test_apply_patch_is_dropped_for_read_only_requests_by_default(self):
        normalized = normalize_request(
            {
                "model": "deepseek-reviewer",
                "tools": [
                    {
                        "type": "function",
                        "name": "apply_patch",
                        "description": "patch",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            }
        )

        self.assertEqual(normalized.tools, [])
        self.assertEqual(normalized.dropped_tools, ["apply_patch"])

    def test_tool_choice_none_is_permitted(self):
        normalized = normalize_request(
            {"model": "deepseek-chat", "input": "hi", "tools": [], "tool_choice": None}
        )
        self.assertEqual(normalized.messages[-1]["content"], "hi")

    def test_tool_choice_auto_is_permitted(self):
        normalized = normalize_request(
            {"model": "deepseek-chat", "input": "hi", "tools": [], "tool_choice": "auto"}
        )
        self.assertEqual(normalized.messages[-1]["content"], "hi")

    def test_tool_choice_required_is_rejected(self):
        with self.assertRaises(PayloadNormalizationError):
            normalize_request(
                {"model": "deepseek-chat", "input": "hi", "tools": [], "tool_choice": "required"}
            )

    def test_tool_choice_specific_function_is_rejected(self):
        with self.assertRaises(PayloadNormalizationError):
            normalize_request(
                {
                    "model": "deepseek-chat",
                    "input": "hi",
                    "tools": [],
                    "tool_choice": {"type": "function", "name": "exec_command"},
                }
            )

    def test_apply_patch_is_preserved_for_worker_alias(self):
        normalized = normalize_request(
            {
                "model": "deepseek-worker",
                "tools": [
                    {
                        "type": "function",
                        "name": "apply_patch",
                        "description": "patch",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            }
        )

        self.assertEqual(normalized.normalized_tool_names, ["apply_patch"])
        self.assertEqual(normalized.dropped_tools, [])

    def test_allow_apply_patch_disabled_kill_switch_overrides_worker_model(self):
        """allow_apply_patch_enabled=False must drop apply_patch even for deepseek-worker."""
        normalized = normalize_request(
            {
                "model": "deepseek-worker",
                "tools": [
                    {
                        "type": "function",
                        "name": "apply_patch",
                        "description": "patch",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            },
            allow_apply_patch_enabled=False,
        )

        self.assertEqual(normalized.tools, [])
        self.assertEqual(normalized.dropped_tools, ["apply_patch"])

    def test_apply_patch_is_preserved_when_explicitly_enabled(self):
        with patch.dict("os.environ", {"DEEPSEEK_ALLOW_APPLY_PATCH": "1"}):
            normalized = normalize_request(
                {
                    "model": "deepseek-reviewer",
                    "tools": [
                        {
                            "type": "function",
                            "name": "apply_patch",
                            "description": "patch",
                            "parameters": {"type": "object", "properties": {}},
                        }
                    ],
                }
            )

        self.assertEqual(normalized.normalized_tool_names, ["apply_patch"])

    def test_apply_patch_env_is_ignored_when_setting_is_explicitly_disabled(self):
        with patch.dict("os.environ", {"DEEPSEEK_ALLOW_APPLY_PATCH": "1"}):
            normalized = normalize_request(
                {
                    "model": "deepseek-reviewer",
                    "tools": [
                        {
                            "type": "function",
                            "name": "apply_patch",
                            "description": "patch",
                            "parameters": {"type": "object", "properties": {}},
                        }
                    ],
                },
                allow_apply_patch_enabled=False,
            )

        self.assertEqual(normalized.tools, [])
        self.assertEqual(normalized.dropped_tools, ["apply_patch"])

    def test_namespace_tools_are_dropped_by_default(self):
        normalized = normalize_request(
            {
                "model": "deepseek-reviewer",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "mcp__playwright__",
                        "tools": [
                            {
                                "type": "function",
                                "name": "browser_snapshot",
                                "description": "snapshot",
                                "parameters": {"type": "object", "properties": {}},
                            }
                        ],
                    }
                ],
            }
        )

        self.assertEqual(normalized.tools, [])
        self.assertEqual(normalized.dropped_tools, ["mcp__playwright____browser_snapshot"])

    def test_strict_true_is_forwarded_only_for_matching_required_properties(self):
        normalized = normalize_request(
            {
                "tools": [
                    {
                        "type": "function",
                        "name": "strict_ok",
                        "description": "ok",
                        "strict": True,
                        "parameters": {
                            "type": "object",
                            "properties": {"cmd": {"type": "string"}},
                            "required": ["cmd"],
                        },
                    },
                    {
                        "type": "function",
                        "name": "strict_bad",
                        "description": "bad",
                        "strict": True,
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "cmd": {"type": "string"},
                                "workdir": {"type": "string"},
                            },
                            "required": ["cmd"],
                        },
                    },
                ]
            }
        )

        self.assertTrue(normalized.tools[0]["function"]["strict"])
        self.assertNotIn("strict", normalized.tools[1]["function"])

    def test_tool_call_loop_preserves_call_id(self):
        normalized = normalize_request(
            {
                "model": "deepseek-chat",
                "input": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": "pwd"}),
                        "call_id": "call_123",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_123",
                        "output": "ok",
                    },
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "continue"}],
                    },
                ],
                "tools": [],
            }
        )

        self.assertEqual(normalized.messages[0]["role"], "assistant")
        self.assertEqual(normalized.messages[0]["tool_calls"][0]["id"], "call_123")
        self.assertEqual(normalized.messages[1], {"role": "tool", "tool_call_id": "call_123", "content": "ok"})

    def test_replayed_function_call_reattaches_reasoning_content(self):
        normalized = normalize_request(
            {
                "model": "deepseek-reviewer",
                "input": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": "pwd"}),
                        "call_id": "call_123",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_123",
                        "output": "ok",
                    },
                ],
            },
            reasoning_content_by_call_id={"call_123": "private chain of thought"},
        )

        self.assertEqual(normalized.messages[0]["reasoning_content"], "private chain of thought")
        self.assertEqual(normalized.messages[1], {"role": "tool", "tool_call_id": "call_123", "content": "ok"})

    def test_deepseek_v4_replay_adds_fallback_reasoning_content_when_cache_is_missing(self):
        normalized = normalize_request(
            {
                "model": "deepseek-reviewer",
                "input": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "I will inspect the diff."}],
                    },
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": "pwd"}),
                        "call_id": "call_123",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_123",
                        "output": "ok",
                    },
                ],
            }
        )

        self.assertEqual(
            normalized.messages[0]["reasoning_content"],
            "Reasoning content was not retained by the local Responses proxy.",
        )
        self.assertEqual(
            normalized.messages[1]["reasoning_content"],
            "Reasoning content was not retained by the local Responses proxy.",
        )

    def test_deepseek_chat_replay_does_not_add_reasoning_content(self):
        normalized = normalize_request(
            {
                "model": "deepseek-chat",
                "input": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "I will inspect the diff."}],
                    }
                ],
            }
        )

        self.assertNotIn("reasoning_content", normalized.messages[0])

    def test_malformed_tool_history_fails(self):
        with self.assertRaises(PayloadNormalizationError):
            normalize_request(
                {
                    "model": "deepseek-chat",
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
                }
            )

    def test_previous_response_state_allows_delta_tool_output(self):
        normalized = normalize_request(
            {
                "model": "deepseek-chat",
                "previous_response_id": "resp_1",
                "instructions": "already present in previous state",
                "input": [
                    {
                        "type": "function_call_output",
                        "call_id": "call_123",
                        "output": "ok",
                    }
                ],
            },
            previous_messages=[
                {"role": "system", "content": "already present in previous state"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {"name": "exec_command", "arguments": "{}"},
                        }
                    ],
                },
            ],
        )

        self.assertEqual(normalized.messages[-1], {"role": "tool", "tool_call_id": "call_123", "content": "ok"})

    def test_tool_output_without_prior_call_fails(self):
        with self.assertRaises(PayloadNormalizationError):
            normalize_request(
                {
                    "model": "deepseek-chat",
                    "input": [
                        {
                            "type": "function_call_output",
                            "call_id": "call_123",
                            "output": "ok",
                        }
                    ],
                }
            )

    def test_redacts_secrets(self):
        self.assertEqual(
            redact({"Authorization": "Bearer sk-test", "nested": {"api_key": "secret"}}),
            {"Authorization": "[REDACTED]", "nested": {"api_key": "[REDACTED]"}},
        )
        self.assertEqual(
            redact({"arguments": '{"cmd":"env","env":{"TOKEN":"secret"}}'}),
            {"arguments": '{"cmd":"env","env":"[REDACTED]"}'},
        )


if __name__ == "__main__":
    unittest.main()
