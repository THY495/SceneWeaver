import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from chatbot.directing import PerformanceJob
from chatbot.npc.assignment import authorize_assignment
from chatbot.npc.context import TurnContext
from chatbot.providers import ProviderError


@pytest.mark.parametrize("accepted,supported", [(True, True), (False, True), (True, False)])
async def test_assignment_review_only_forwards_accepted_goal(accepted, supported):
    info = {
        "perceived_cast": [{"id": "a", "name": "甲"}, {"id": "p", "name": "玩家"}],
        "profile": {"core": "尚不知道钥匙在哪里"},
    }
    original = deepcopy(info)
    async def answer(_task, _role, _messages, *, operation, **_kwargs):
        if "goal-repair" in operation:
            return {"objective": "仅询问玩家的意见", "delivery_constraints": ["一到两句"]}
        return {
            "accepted": accepted, "issues": [],
            "checks": [{"text": "预设", "basis": "检查依据", "supported": supported}],
        }

    provider = SimpleNamespace(chat=AsyncMock(side_effect=answer))
    services = SimpleNamespace(
        runtime=SimpleNamespace(check=Mock()),
        provider=provider,
        context=SimpleNamespace(assemble=Mock(return_value=([], info))),
    )
    task = SimpleNamespace(id="t", lease_version=1)
    job = PerformanceJob(
        id="j",
        actor="a",
        objective="询问玩家的意见",
        addressees=["p"],
        delivery_constraints=["一到两句"],
    )
    args = (services, task, TurnContext("s", "p", ("a",)), SimpleNamespace(id="a"), job)
    if accepted and supported:
        assert await authorize_assignment(*args, operation_scope="test:") == {
            "objective": job.objective,
            "addressees": ["p"],
            "delivery_constraints": ["一到两句"],
        }
    else:
        with pytest.raises(ProviderError, match="知情边界"):
            await authorize_assignment(*args, operation_scope="test:")
    assert info == original
    proposal = json.loads(provider.chat.call_args_list[0].args[2][1]["content"])
    assert proposal["performer_id"] == "a"
    assert proposal["assignment"]["addressees"] == ["p"]
    assert proposal["assignment"]["delivery_constraints"] == ["一到两句"]
    job.addressees = ["hidden"]
    with pytest.raises(ProviderError, match="不可感知"):
        await authorize_assignment(*args, operation_scope="test:")
    assert provider.chat.await_count == (1 if accepted and supported else 3)


async def test_assignment_approval_with_issues_is_rechecked_before_actor_sees_it():
    info = {"perceived_cast": [{"id": "a", "name": "甲"}, {"id": "p", "name": "玩家"}]}
    provider = SimpleNamespace(
        chat=AsyncMock(
            side_effect=[
                {
                    "accepted": True,
                    "issues": ["若要确认未读过的公告原文，需要再核实。"],
                    "checks": [{"text": "公告原文", "basis": "任务未要求确认原文", "supported": False}],
                },
                {"accepted": True, "issues": [], "checks": []},
            ]
        )
    )
    services = SimpleNamespace(
        runtime=SimpleNamespace(check=Mock()),
        provider=provider,
        context=SimpleNamespace(assemble=Mock(return_value=([], info))),
    )
    job = PerformanceJob(
        id="j", actor="a", objective="回应玩家的转述，不确认公告原文", addressees=["p"],
        delivery_constraints=["未知就承认未知"],
    )
    result = await authorize_assignment(
        services, SimpleNamespace(id="t", lease_version=1),
        TurnContext("s", "p", ("a",)), SimpleNamespace(id="a"), job, operation_scope="test:",
    )
    assert result["objective"] == job.objective
    assert provider.chat.await_count == 2
    assert "format-repair" in provider.chat.await_args.kwargs["operation"]


async def test_rejected_assignment_is_rewritten_once_and_rechecked_before_actor_sees_it():
    info = {
        "perceived_cast": [{"id": "a", "name": "甲"}, {"id": "p", "name": "玩家"}],
        "profile": {"core": "没有读过登记册"},
    }

    async def answer(_task, _role, messages, *, operation, **_kwargs):
        if "goal-repair" in operation:
            assert "没有读过登记册" in messages[1]["content"]
            return {
                "objective": "回应玩家的问题，仅说自己实际知道的部分，不知道就承认未知",
                "delivery_constraints": ["一到两句"],
            }
        proposal = json.loads(messages[1]["content"])["assignment"]
        if "亲眼读过登记册" in proposal["objective"]:
            return {
                "accepted": False,
                "issues": ["没有依据证明演员读过登记册"],
                "checks": [{"text": "亲眼读过登记册", "basis": "授权状态无此记录", "supported": False}],
            }
        return {"accepted": True, "issues": [], "checks": []}

    provider = SimpleNamespace(chat=AsyncMock(side_effect=answer))
    services = SimpleNamespace(
        runtime=SimpleNamespace(check=Mock()),
        provider=provider,
        context=SimpleNamespace(assemble=Mock(return_value=([], info))),
    )
    job = PerformanceJob(
        id="j", actor="a", objective="说明自己亲眼读过登记册后发现了什么",
        addressees=["p"], delivery_constraints=["一到两句"],
    )
    result = await authorize_assignment(
        services, SimpleNamespace(id="t", lease_version=1),
        TurnContext("s", "p", ("a",)), SimpleNamespace(id="a"), job, operation_scope="test:",
    )
    assert result == {
        "objective": "回应玩家的问题，仅说自己实际知道的部分，不知道就承认未知",
        "addressees": ["p"],
        "delivery_constraints": ["一到两句"],
    }
    assert job.objective == "说明自己亲眼读过登记册后发现了什么"
    assert [call.args[1] for call in provider.chat.call_args_list] == [
        "director_assignment", "director_assignment_repair", "director_assignment"
    ]
