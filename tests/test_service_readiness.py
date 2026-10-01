import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from chatbot.service_readiness import ModelReadiness


def test_missing_key_blocks_game_entry_without_creating_world(config, db, fake):
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from chatbot.api import create_app
    from chatbot.storage import Story, Task

    config.product = "roleplay"
    config.api_key = SecretStr("")
    with TestClient(create_app(config, db, fake)) as client:
        assert client.get("/api/runtime/readiness").json()["llm"]["configured"] is False
        result = client.post(
            "/api/role/experiments/start",
            json={"request_id": "no-key", "template_id": "partial-hearing"},
        )
        assert result.status_code == 409
        assert client.post("/api/runtime/check").status_code == 409
        with db.session() as s:
            assert s.scalar(select(func.count()).select_from(Story)) == 0
            assert s.scalar(select(func.count()).select_from(Task)) == 0


@pytest.mark.asyncio
async def test_requires_key_checks_model_and_invalidates_on_change(config, db):
    class Probe:
        calls = 0
        fail = False

        async def chat(self, *args, **kwargs):
            self.calls += 1
            if self.fail:
                raise RuntimeError("private provider error")
            return "OK"

    probe = Probe()
    gate = ModelReadiness(config, db, probe)
    config.api_key = SecretStr("")
    with pytest.raises(HTTPException) as error:
        await gate.require()
    assert error.value.status_code == 409 and probe.calls == 0
    config.api_key = SecretStr("test")
    await gate.require()
    await gate.require()
    assert probe.calls == 2  # Dialogue and expert use distinct configured models.
    config.dialogue_model = "changed"
    probe.fail = True
    with pytest.raises(HTTPException) as error:
        await gate.require()
    assert "private" not in error.value.detail
    assert probe.calls == 3 and gate.until == 0


async def test_native_release_readiness_before_first_save_is_scoped(config, db):
    import httpx
    from sqlalchemy import select

    from chatbot.game_scope import GameExecutionScope
    from chatbot.providers import Provider
    from chatbot.runtime import Runtime, StaleTask
    from chatbot.storage import Task

    runtime = Runtime(db, config, execution_scope=GameExecutionScope(config.workspace_owner, "release-a"))
    provider = Provider(db, config, runtime)
    await provider.http.aclose()
    calls = []

    def endpoint(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}],
                                        "usage": {"prompt_tokens": 2, "completion_tokens": 1}})

    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    try:
        await ModelReadiness(config, db, provider).require()
        assert len(calls) == 2
        with db.session.begin() as s:
            task = s.scalar(select(Task).where(Task.kind == "connection_check"))
            assert task.status == "completed" and task.session_id is None
            assert task.payload == {"release_id": "release-a"}
            task.status = "running"
            tid = task.id
        assert runtime.check(tid).id == tid
        other = Runtime(db, config, execution_scope=GameExecutionScope(config.workspace_owner, "release-b"))
        with pytest.raises(StaleTask, match="当前发行作品"):
            other.check(tid)
        with db.session.begin() as s:
            s.get(Task, tid).payload = {}
        with pytest.raises(StaleTask, match="当前发行作品"):
            runtime.check(tid)
    finally:
        await provider.close()
