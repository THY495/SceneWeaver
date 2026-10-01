"""Bounded generated result explanations; never replaces the independent fact review."""

from ..provenance import source_class
from ..providers import ProviderError


def result_candidates(info):
    """Only expose result records already present in this actor's working set."""
    return [
        {"id": event["id"], "text": event["text"]}
        for event in info.get("observations", [])
        if source_class(event) == "item_action_result"
    ]


def result_focus(intent, info):
    event_id = intent.get("result_event_id")
    if not event_id:
        return None
    if intent.get("act") != "speak":
        raise ProviderError("结果说明只能用于发言，不能同时执行物品操作。")
    event = next((e for e in info.get("observations", []) if e["id"] == event_id), None)
    if not event or source_class(event) != "item_action_result":
        raise ProviderError("结果说明必须引用此角色实际观察到的物品操作结果。")
    return event


def bind_result_quotes(speech, event):
    """Bind a scoped result citation; factual support still requires semantic review."""
    if not event or not 0 < len(event.get("text", "")) <= 1500:
        return speech
    return {
        **speech,
        "claims": [
            {**claim, "quote": event["text"]} if claim.get("source_id") == event["id"] else claim
            for claim in speech.get("claims", [])
        ],
    }


def expression_token_budget(focused_result, info):
    # Full result explanations include claims, spoken_parts and performance as well as text.
    # Keep extra headroom bounded and limited to turns with observed item results.
    return 600 if focused_result else 2000 if result_candidates(info) else 1200


RESULT_EXPRESSION = (
    "本片段仅用一到两句角色台词说明下面这次已发生操作的结果，通常不超过60个汉字。"
    "使用人物日常语言，不出现系统、返回结果、规则、协议、版本、校验等后台措辞。"
    "不添加括号动作、外观细节或新机制，不重复完整报错，不虚构原因；不清楚的部分可以简短承认不知道。"
    "失败用我没能/还打不开等自然说法；成功只说实际完成的部分。保持角色语气，不增加无关承诺或追问。"
    "依然输出完整ActorSpeech结构和必要claims，引用指定结果事件的原文；禁止把失败说成成功。"
)

RESULT_LANGUAGE = (
    "解释物品操作结果时，把记录中的后台措辞转换为人物自己的语言。"
    "例如上锁只支持说明还打不开、它锁着，不要说操作被拒绝、系统提示、满足解锁条件。"
    "只知道尝试失败，不代表实际拉动、扳动或查看过锁扣；没有记录就不要添加这些动作。"
    "可以承认不知道怎样打开。若玩家同时有其他问题或要求，仍处理完整请求，不因结果说明而遗漏。"
)
