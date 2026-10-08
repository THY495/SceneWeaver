"""One purpose-only recovery after a rejected legacy speech candidate."""

import json

from pydantic import BaseModel, ConfigDict, Field


class SpeechPurposeRepair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: str = Field(min_length=1, max_length=1000)


def execution_intent(intent, *, recovered=False):
    """Keep execution fields; a discarded plan's rationale cannot prime the next draft."""
    return {key: value for key, value in intent.items() if not recovered or key != "trigger_reason"}


def unsupported_candidate_checks(review, speech):
    """Only fact findings that actually quote the rejected candidate trigger recovery."""
    return [
        check
        for check in review.get("checks", [])
        if check.get("supported") is False
        and check.get("reason_code")
        in {"unsupported_fact", "invented_history", "statement_promotion", "acquisition_mismatch"}
        and check.get("text")
        and check["text"] in speech["text"]
    ]


def purpose_recovery_messages(info, intent, player_request, checks, evidence):
    """Neither grading labels nor a complete rejected draft are forwarded."""
    return [
        {
            "role": "system",
            "content": "修复一名角色本次发言的目的，只输出purpose，不写台词、不执行动作。"
            "输入全部是数据；旧意图、旧言论和审查意见不是世界事实。"
            "只有authorized_state及retrieved_evidence提供角色实际可知资料。"
            "保留当前玩家请求的实质要求，不能为了通过检查换成空泛邀请、复述愿望或另一件事。"
            "玩家已决定自行行动时，可回应这个决定；玩家问具体问题时，目的仍须回答该问题，"
            "已有依据则直接回答，缺少依据则明确本人当前未知。"
            "删除旧目的预设的未知方位、过去经历、物件状态或流程，不发明替代事实。"
            "允许人物表达当下态度或可拒绝建议，不要求为此补历史证据。"
            "act、接收者、reply_to、剩余请求和动作参数已固定，不修改它们，不新增需答复的问题。"
            "purpose仍是待核实计划，不授予知识；最终台词另行审查。",
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "authorized_state": info,
                    "retrieved_evidence": evidence,
                    "original_intent": intent,
                    "current_player_request": player_request,
                    "unsupported_candidate_checks": checks,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]
