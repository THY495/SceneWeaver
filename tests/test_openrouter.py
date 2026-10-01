import json

import httpx
import pytest
from pydantic import BaseModel, SecretStr
from sqlalchemy import select

from chatbot.providers import Provider, ProviderError
from chatbot.storage import Operation, Usage


class Verdict(BaseModel):
    accepted: bool


async def test_fixed_official_route_and_actual_cost(db, config, runtime):
    config.openrouter_api_key = SecretStr("test-or-key")
    config.openrouter_roles = {"actor_review": "test/model"}
    config.openrouter_providers = {"test/model": ["fixed"]}
    config.openrouter_prices = {"test/model": {"input": 2, "output": 10}}
    session = runtime.create_session()
    runtime.submit(session.id, "local", "test", "1")
    task = runtime.claim("w")
    provider = Provider(db, config, runtime)

    def handler(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        body = json.loads(request.content)
        assert body["provider"]["allow_fallbacks"] is False
        assert body["provider"]["require_parameters"] is True
        assert body["provider"]["max_price"] == {"prompt": 2, "completion": 10, "request": 0}
        assert body["response_format"]["type"] == "json_schema"
        return httpx.Response(
            200,
            json={
                "id": "generation-1",
                "provider": "fixed",
                "model": "test/model",
                "choices": [{"message": {"content": '{"accepted":false}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.00123},
            },
        )

    await provider.http.aclose()
    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await provider.chat(
        task.id, "actor_review", [{"role": "user", "content": "test"}], schema=Verdict
    ) == {"accepted": False}
    with db.session() as s:
        usage = s.scalar(select(Usage))
        assert usage.cost == 0.00123 and usage.reserved == 0
        saved = s.get(Operation, f"usage:{usage.id}").payload
        assert saved["price"] == [2, 2, 10] and saved["provider"] == "fixed"
    await provider.close()


async def test_unpriced_route_never_calls_network(db, config, runtime):
    config.openrouter_api_key = SecretStr("test-or-key")
    config.openrouter_roles = {"dialogue": "unknown/model"}
    session = runtime.create_session()
    runtime.submit(session.id, "local", "test", "1")
    task = runtime.claim("w")
    provider = Provider(db, config, runtime)
    with pytest.raises(ProviderError, match="价格快照"):
        await provider.chat(task.id, "dialogue", [{"role": "user", "content": "test"}])
    with db.session() as s:
        assert s.scalar(select(Usage)) is None
    await provider.close()


def test_missing_provider_cost_keeps_reservation(db, config, runtime):
    session = runtime.create_session()
    runtime.submit(session.id, "local", "test", "1")
    task = runtime.claim("w")
    provider = Provider(db, config, runtime)
    uid = provider.reserve(task.id, "actor_review", "test/model", "test", 100, price=(2, 2, 10))
    with db.session() as s:
        reserved = s.get(Usage, uid).reserved
    provider.settle(uid, {"prompt_tokens": 1, "completion_tokens": 1}, 1, "completed")
    with db.session() as s:
        row = s.get(Usage, uid)
        assert row.cost == reserved and row.status == "estimated_completed"
