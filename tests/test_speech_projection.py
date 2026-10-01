from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chatbot.npc.speech_projection import project_speech
from chatbot.providers import BudgetExceeded, ProviderError
from chatbot.runtime import StaleTask


async def project(speech, value=None, error=None, repair=True):
    provider = SimpleNamespace(chat=AsyncMock(return_value=value, side_effect=error))
    original = deepcopy(speech)
    result = await project_speech(provider, "task", speech, operation="projection:0", repair=repair)
    assert speech == original
    assert result[0]["text"] == original["text"]
    assert result[0]["claims"] == original["claims"]
    return result, provider


def draft(parts=None):
    return {
        "text": "（点头）我想聊旅行。你呢？",
        "spoken_parts": parts or [],
        "claims": [{"text": "我想聊旅行。", "mode": "proposal"}],
        "performance": [],
    }


async def test_missing_parts_repaired_once_without_changing_grounded_content():
    (speech, failed), provider = await project(
        draft(), {"has_dialogue": True, "spoken_parts": ["我想聊旅行。你呢？"]}
    )
    assert not failed and speech["spoken_parts"] == ["我想聊旅行。你呢？"]
    provider.chat.assert_awaited_once()
    assert provider.chat.call_args.kwargs["operation"] == "projection:0"


@pytest.mark.parametrize(
    "value",
    [
        {"has_dialogue": True, "spoken_parts": ["编造的内容"]},
        {"has_dialogue": True, "spoken_parts": ["点头"]},
        {"has_dialogue": True, "spoken_parts": ["你呢？", "我想聊旅行。"]},
        {"has_dialogue": True, "spoken_parts": []},
        {"has_dialogue": False, "spoken_parts": ["我想聊旅行。"]},
        {"has_dialogue": True, "spoken_parts": ["我想聊旅行。"], "text": "改写"},
    ],
)
async def test_invalid_projection_is_explicit_failure_not_silent_success(value):
    (speech, failed), provider = await project(draft(["不在正文"]), value)
    assert failed and speech["spoken_parts"] == [] and speech["performance"] == []
    provider.chat.assert_awaited_once()


async def test_action_only_can_remain_silent():
    speech = {**draft(), "text": "（点头）", "claims": []}
    (result, failed), _ = await project(speech, {"has_dialogue": False, "spoken_parts": []})
    assert not failed and result["spoken_parts"] == []


async def test_valid_projection_and_disabled_voice_do_not_add_model_calls():
    (_, failed), provider = await project(draft(["我想聊旅行。你呢？"]))
    assert not failed
    provider.chat.assert_not_awaited()
    (_, failed), provider = await project(draft(), repair=False)
    assert not failed
    provider.chat.assert_not_awaited()


async def test_provider_failure_is_bounded_text_fallback():
    (result, failed), provider = await project(draft(), error=ProviderError("unavailable"))
    assert failed and result["spoken_parts"] == []
    provider.chat.assert_awaited_once()


@pytest.mark.parametrize("error", [BudgetExceeded("budget"), StaleTask("stale")])
async def test_budget_and_task_invalidation_are_not_swallowed(error):
    with pytest.raises(type(error)):
        await project(draft(), error=error)
