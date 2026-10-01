"""The optional Responses reviewer must preserve fail-closed review and usage accounting."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from chatbot.config import Settings
from chatbot.observability.timing import TimingTrace
from chatbot.providers import Provider, ProviderError
from chatbot.schemas import ActorReview


def provider_with_transport(handler, *, output_format="responses_json_schema"):
    provider = Provider.__new__(Provider)
    provider.config = Settings(
        _env_file=None,
        api_key="sk-test",
        model_provider="deepseek",
        dialogue_model="deepseek-v4-flash",
        deepseek_review_format=output_format,
    )
    provider.runtime = SimpleNamespace(check=lambda *args: SimpleNamespace(lease_version=1))
    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider.slots = asyncio.Semaphore(2)
    provider.background_slots = asyncio.Semaphore(1)
    provider.ack_slots = asyncio.Semaphore(1)
    provider.reserve = lambda *args, **kwargs: "usage-test"
    settled = []
    provider.settle = lambda *args, **kwargs: settled.append((args, kwargs))
    return provider, settled


@pytest.mark.asyncio
async def test_deepseek_responses_review_uses_schema_and_normalizes_usage():
    captured = []

    def handler(request):
        body = json.loads(request.content)
        captured.append((request.url.path, body))
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "private"}]},
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "accepted": True,
                                        "checks": [],
                                        "issues": [],
                                        "delivery": {"expects_reply": False},
                                    }
                                ),
                            }
                        ],
                    },
                ],
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "input_tokens_details": {"cached_tokens": 50},
                },
            },
        )

    provider, settled = provider_with_transport(handler)
    timestamp = datetime.now(UTC)
    trace = TimingTrace(
        SimpleNamespace(
            id="task",
            attempt=1,
            lease_version=1,
            created_at=timestamp,
            updated_at=timestamp,
        )
    )
    try:
        with trace.activate():
            review = await provider.chat(
                "task",
                "actor_review",
                [{"role": "user", "content": "审查候选"}],
                schema=ActorReview,
                max_tokens=1000,
            )
    finally:
        await provider.http.aclose()
    assert review["accepted"] is True
    assert "private" not in str(review)
    assert captured[0][0] == "/responses"
    body = captured[0][1]
    assert body["model"] == "deepseek-flash"
    assert body["reasoning"] == {"effort": "none"}
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["schema"] == ActorReview.model_json_schema()
    assert trace.requests[0]["response_format"] == "json_schema"
    assert trace.requests[0]["max_output_tokens"] == 1000
    assert settled[0][0][1] == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "prompt_cache_hit_tokens": 50,
    }
    assert settled[0][0][3] == "completed"


@pytest.mark.asyncio
async def test_deepseek_responses_incomplete_review_fails_closed_and_accounts_usage():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": '{"accepted":true'}],
                    }
                ],
                "usage": {"input_tokens": 100, "output_tokens": 1000},
            },
        )

    provider, settled = provider_with_transport(handler)
    try:
        with pytest.raises(ProviderError, match="输出上限"):
            await provider.chat(
                "task",
                "actor_review",
                [{"role": "user", "content": "审查候选"}],
                schema=ActorReview,
                max_tokens=1000,
            )
    finally:
        await provider.http.aclose()
    assert len(settled) == 1
    assert settled[0][0][1]["completion_tokens"] == 1000
    assert settled[0][0][3] == "failed"


@pytest.mark.asyncio
async def test_default_reviewer_still_uses_chat_completions_json_mode():
    captured = []

    def handler(request):
        captured.append((request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "accepted": True,
                                    "checks": [],
                                    "issues": [],
                                    "delivery": {"expects_reply": False},
                                }
                            )
                        },
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20},
            },
        )

    provider, _ = provider_with_transport(handler, output_format="json_object")
    try:
        review = await provider.chat(
            "task",
            "actor_review",
            [{"role": "user", "content": "审查候选"}],
            schema=ActorReview,
            max_tokens=1000,
        )
    finally:
        await provider.http.aclose()
    assert review["accepted"] is True
    assert captured[0][0] == "/chat/completions"
    assert captured[0][1]["response_format"] == {"type": "json_object"}
