import asyncio
import unittest
from unittest.mock import patch

from subagent_router.normalization import normalize_request
from subagent_router.providers import ProviderCapabilities, ProviderConfig
from subagent_router.providers.openai_compat import OpenAICompatibleProvider


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


class FakeAsyncClient:
    posted_json = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, *args, **kwargs):
        FakeAsyncClient.posted_json = kwargs["json"]
        return FakeResponse()


class ProviderTests(unittest.TestCase):
    def test_openai_compatible_forwards_parallel_tool_calls_when_enabled(self):
        payload = {
            "model": "deepseek-chat",
            "parallel_tool_calls": True,
            "input": "use a tool",
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
        provider = OpenAICompatibleProvider(
            ProviderConfig(
                name="deepseek",
                kind="cloud",
                provider_type="deepseek",
                base_url="http://localhost:9000/v1",
                api_key="sk-test",
                capabilities=ProviderCapabilities(supports_parallel_tool_calls=True),
                send_parallel_tool_calls=True,
            )
        )

        with patch("subagent_router.providers.openai_compat.httpx.AsyncClient", FakeAsyncClient):
            asyncio.run(provider.chat(normalized))

        self.assertTrue(FakeAsyncClient.posted_json["parallel_tool_calls"])


if __name__ == "__main__":
    unittest.main()
