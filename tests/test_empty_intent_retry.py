"""Bounded opt-in intent recovery never interprets empty output as a decision."""

import json
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from chatbot.observability.timing import TimingTrace
from chatbot.providers import BudgetExceeded, EmptyStructuredOutput, OutputTruncated, Provider, ProviderError
from chatbot.runtime import StaleTask
from chatbot.schemas import ActorIntent
from chatbot.storage import Conversation, Operation, Task, Usage, now


def task_and_provider(db, config, runtime):
    config.model_provider = "deepseek"
    config.api_base = "https://api.deepseek.com"
    conversation = runtime.create_session()
    runtime.submit(conversation.id, "local", "Hello", "empty-intent-test")
    return runtime.claim("empty-intent-worker"), Provider(db, config, runtime)


def frame(content="", reason="stop", **message_fields):
    return httpx.Response(
        200,
        json={
            "choices": [{"finish_reason": reason, "message": {"content": content, **message_fields}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 87},
        },
    )


async def test_empty_intent_recovers_once_with_identical_request_and_settled_attempts(db, config, runtime):
    task, provider = task_and_provider(db, config, runtime)
    calls, trace = [], TimingTrace(task)
    answer = ActorIntent(act="speak", purpose="回应当前问题", retrieval="context").model_dump()

    def respond(request):
        calls.append(json.loads(request.content))
        return frame("  ") if len(calls) == 1 else frame(json.dumps(answer))

    await provider.http.aclose()
    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        with trace.activate():
            result = await provider.chat(
                task.id,
                "role_intent",
                [{"role": "user", "content": "test"}],
                schema=ActorIntent,
                operation="bounded-empty-test",
                retry_empty_json=True,
            )
        assert result == answer and calls[0] == calls[1]
        assert len(calls) == 2
        assert any(s["name"] == "model.retry_wait" and "empty_json" in s["marks"] for s in trace.spans)
        with db.session() as session:
            usage = list(
                session.scalars(select(Usage).where(Usage.task_id == task.id).order_by(Usage.created_at))
            )
            assert [u.status for u in usage] == ["failed", "completed"]
            assert all(u.output_tokens == 87 and u.reserved == 0 for u in usage)
            cached = session.get(Operation, task.id + ":bounded-empty-test")
            assert cached.payload["value"] == answer
    finally:
        await provider.close()


@pytest.mark.parametrize("enabled,attempts,count", [(False, 2, 1), (True, 1, 1), (True, 2, 2)])
async def test_repeated_empty_never_caches_and_shares_original_attempt_limit(
    db, config, runtime, enabled, attempts, count
):
    task, provider = task_and_provider(db, config, runtime)
    calls = []

    def respond(request):
        calls.append(request)
        return frame(None)

    await provider.http.aclose()
    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(EmptyStructuredOutput):
            await provider.chat(
                task.id,
                "role_intent",
                [{"role": "user", "content": "test"}],
                schema=ActorIntent,
                operation="bounded-empty-test",
                max_attempts=attempts,
                retry_empty_json=enabled,
            )
        assert len(calls) == count
        with db.session() as session:
            assert session.get(Operation, task.id + ":bounded-empty-test") is None
            usage = list(session.scalars(select(Usage).where(Usage.task_id == task.id)))
            assert len(usage) == count and all(u.status == "failed" and u.reserved == 0 for u in usage)
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "case",
    [
        "filter",
        "length",
        "unknown",
        "refusal",
        "reasoning",
        "no_schema",
        "wrong_role",
        "wrong_provider",
        "custom_endpoint",
        "bad_type",
        "missing_content",
        "thinking",
        "routed",
        "bad_refusal",
        "bad_reasoning",
        "tool_call",
    ],
)
async def test_empty_retry_rejects_ambiguous_or_nonmatching_protocols(db, config, runtime, case):
    task, provider = task_and_provider(db, config, runtime)
    calls = []
    role, schema = "role_intent", ActorIntent
    if case == "no_schema":
        schema = None
    if case == "wrong_role":
        role = "acknowledgement"
    if case == "wrong_provider":
        config.model_provider = "openai"
    if case == "custom_endpoint":
        config.api_base = "https://fixture.invalid"
    if case == "routed":
        config.openrouter_roles = {role: "fixture/model"}
        config.openrouter_api_key = SecretStr("test-key")
        config.openrouter_prices = {"fixture/model": {"input": 0.5, "output": 1}}
        config.openrouter_providers = {"fixture/model": ["fixture"]}

    def respond(request):
        calls.append(request)
        if case == "missing_content":
            return httpx.Response(
                200,
                json={
                    "choices": [{"finish_reason": "stop", "message": {}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 0},
                },
            )
        return frame(
            False if case == "bad_type" else "",
            "content_filter"
            if case == "filter"
            else "length"
            if case == "length"
            else None
            if case == "unknown"
            else "stop",
            **(
                {"refusal": "private refusal"}
                if case == "refusal"
                else {"reasoning_content": "private reasoning"}
                if case == "reasoning"
                else {"refusal": False}
                if case == "bad_refusal"
                else {"reasoning_content": []}
                if case == "bad_reasoning"
                else {"tool_calls": []}
                if case == "tool_call"
                else {}
            ),
        )

    await provider.http.aclose()
    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(OutputTruncated if case == "length" else ProviderError):
            await provider.chat(
                task.id,
                role,
                [{"role": "user", "content": "test"}],
                schema=schema,
                retry_empty_json=True,
                thinking=case == "thinking",
            )
        assert len(calls) == 1
    finally:
        await provider.close()


async def test_unknown_usage_keeps_conservative_cost_for_both_empty_attempts(db, config, runtime):
    task, provider = task_and_provider(db, config, runtime)
    await provider.http.aclose()
    provider.http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}
            )
        )
    )
    try:
        with pytest.raises(EmptyStructuredOutput):
            await provider.chat(
                task.id,
                "role_intent",
                [{"role": "user", "content": "test"}],
                schema=ActorIntent,
                retry_empty_json=True,
            )
        with db.session() as session:
            rows = list(session.scalars(select(Usage).where(Usage.task_id == task.id)))
            assert len(rows) == 2
            assert all(
                row.status == "estimated_failed" and row.cost > 0 and row.reserved == 0 for row in rows
            )
    finally:
        await provider.close()


@pytest.mark.parametrize("invalidated", ["cancel", "epoch", "lease_expiry", "lease_takeover", "budget"])
async def test_retry_wait_cannot_cross_lifecycle_or_budget_boundary(
    db, config, runtime, monkeypatch, invalidated
):
    task, provider = task_and_provider(db, config, runtime)
    calls = []

    def respond(request):
        calls.append(request)
        return frame()

    async def change_during_wait(delay):
        assert delay == 0.5
        if invalidated == "cancel":
            runtime.cancel(task.id, task.owner_id)
        elif invalidated == "budget":
            config.max_calls_per_task = 1
        else:
            with db.session.begin() as session:
                row = session.get(Task, task.id)
                if invalidated == "epoch":
                    session.get(Conversation, row.session_id).epoch += 1
                elif invalidated == "lease_expiry":
                    row.lease_until = now() - timedelta(seconds=1)
                else:
                    row.lease_version += 1
                    row.lease_owner = "replacement-worker"

    monkeypatch.setattr("chatbot.providers.asyncio.sleep", change_during_wait)
    await provider.http.aclose()
    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(BudgetExceeded if invalidated == "budget" else StaleTask):
            await provider.chat(
                task.id,
                "role_intent",
                [{"role": "user", "content": "test"}],
                schema=ActorIntent,
                retry_empty_json=True,
                operation="bounded-empty-test",
            )
        assert len(calls) == 1
        with db.session() as session:
            assert session.get(Operation, task.id + ":bounded-empty-test") is None
            usage = list(session.scalars(select(Usage).where(Usage.task_id == task.id)))
            assert len(usage) == 1 and usage[0].reserved == 0 and usage[0].status == "failed"
    finally:
        await provider.close()


@pytest.mark.parametrize("delta", ["", "{"])
async def test_stream_never_uses_empty_json_recovery(db, config, runtime, delta):
    task, provider = task_and_provider(db, config, runtime)
    calls, deltas = [], []

    def respond(request):
        calls.append(request)
        body = "data: " + json.dumps({"choices": [{"delta": {"content": delta}, "finish_reason": "stop"}]})
        return httpx.Response(200, text=body + "\n\ndata: [DONE]\n\n")

    async def on_delta(value):
        deltas.append(value)

    await provider.http.aclose()
    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(ProviderError):
            await provider.chat(
                task.id,
                "role_intent",
                [{"role": "user", "content": "test"}],
                schema=ActorIntent,
                retry_empty_json=True,
                on_delta=on_delta,
            )
        assert len(calls) == 1 and deltas == ([delta] if delta else [])
    finally:
        await provider.close()


@pytest.mark.parametrize("first_failure", ["network", "format"])
async def test_other_retry_then_empty_cannot_launch_third_attempt(db, config, runtime, first_failure):
    task, provider = task_and_provider(db, config, runtime)
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return (
                httpx.Response(503, text="unavailable")
                if first_failure == "network"
                else frame("invalid json")
            )
        return frame("")

    await provider.http.aclose()
    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(EmptyStructuredOutput):
            await provider.chat(
                task.id,
                "role_intent",
                [{"role": "user", "content": "test"}],
                schema=ActorIntent,
                retry_empty_json=True,
            )
        assert len(calls) == 2
    finally:
        await provider.close()
