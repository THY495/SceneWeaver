"""Valid director scopes must persist and replay without varchar overflow."""

import httpx
import pytest
from sqlalchemy import func, select

from chatbot.providers import Provider, operation_cache_name
from chatbot.runtime import StaleTask
from chatbot.storage import Operation, Task, Usage


async def test_long_director_cache_key_replays_without_billing_again(db, config, runtime):
    sid = runtime.create_session(config.workspace_owner).id
    runtime.submit(sid, config.workspace_owner, "测试。", "long-cache-key")
    task = runtime.claim("test")
    provider = Provider(db, config, runtime)
    await provider.http.aclose()
    dispatched = []

    def respond(request):
        dispatched.append(request)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "完整答复"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10}})

    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    scope = "director:1:" + "answer-player-" + "x" * 66 + ":0:"
    operation = scope + "actor-turn-v3:0:" + "a" * 36 + ":" + "b" * 24 + ":0"
    assert len(f"{task.id}:{operation}") > 200
    try:
        result = await provider.chat(task.id, "dialogue", [{"role": "user", "content": "测试。"}], operation=operation)
        again = await provider.chat(task.id, "dialogue", [{"role": "user", "content": "测试。"}], operation=operation)
        assert result == again == "完整答复" and len(dispatched) == 1
        with db.session() as s:
            records = [o for o in s.scalars(select(Operation).where(Operation.task_id == task.id)) if "value" in o.payload]
            assert len(records) == 1 and len(records[0].id) <= 200
            assert records[0].payload["cache_operation"] == operation
            assert operation_cache_name(records[0]) == operation
            assert s.scalar(select(func.count()).select_from(Usage).where(Usage.task_id == task.id)) == 1
        # A different attempt cannot alias the earlier result or avoid its own charge.
        await provider.chat(task.id, "dialogue", [{"role": "user", "content": "测试。"}], operation=operation + ":next")
        assert len(dispatched) == 2
        runtime.finish(task.id, task.lease_version, {})
        with pytest.raises(StaleTask):
            await provider.chat(task.id, "dialogue", [{"role": "user", "content": "测试。"}], operation=operation)
    finally:
        await provider.close()


async def test_long_save_keeps_lease_and_short_key_compatibility(db, config, runtime):
    sid = runtime.create_session(config.workspace_owner).id
    runtime.submit(sid, config.workspace_owner, "测试。", "lease-cache-key")
    task = runtime.claim("test")
    provider = Provider(db, config, runtime)
    provider._save_operation(task.id, task.lease_version, "short:reply", "原有结果")
    with db.session() as s:
        short = s.get(Operation, f"{task.id}:short:reply")
        assert short.payload == {"value": "原有结果"}
        assert operation_cache_name(short) == f"{task.id}:short:reply"
    with db.session.begin() as s:
        live = s.get(Task, task.id)
        live.lease_version += 1
    with pytest.raises(StaleTask):
        provider._save_operation(task.id, task.lease_version, "director:" + "x" * 220, "过期结果")
    with db.session() as s:
        assert len([o for o in s.scalars(select(Operation).where(Operation.task_id == task.id)) if "value" in o.payload]) == 1
    await provider.close()
