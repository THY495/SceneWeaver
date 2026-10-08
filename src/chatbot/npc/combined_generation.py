"""Bounded first experiment: one call for a directed, context-grounded performance."""

import json
from hashlib import sha256

from .speech_contract import performance_policy


def basis_digest(info, messages=None):
    """Invalidate a private draft when its actual knowledge/scene basis changes."""
    basis = {
        key: info.get(key)
        for key in (
            "profile", "observations", "scene_facts", "active_goals", "pending_replies",
            "known_objects", "perceived_cast",
            "known_agreements", "privacy_version", "snapshot_id", "memory_ids", "actor_revision",
            "player_statement_history",
        )
    }
    if messages is not None:
        # Includes the actual selected relationships, memories and summary,
        # rather than assuming their IDs alone imply unchanged content.
        basis["working_set_messages"] = messages
    return sha256(json.dumps(basis, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def fallback_reason(task, info, director_assignment):
    if director_assignment is None:
        return "not_directed"
    if task.payload.get("handoff_proposal_id") or task.payload.get("item_result_event_id"):
        return "item_protocol"
    if info["profile"].get("document_ids"):
        # Retrieval is currently chosen by the intent. Never draft first and use
        # later retrieval as if the actor had already consulted those documents.
        return "knowledge_documents"
    return None


def joint_intent_instruction():
    """A directed unit has one owner; legacy whole-turn continuation is separate."""
    return (
        "\n本次同时决定这一演出片段的意图并表达台词。选择speak、wait、inspect或明确的世界动作。"
        "决定是否回应以及同意、拒绝或暂缓时，依据本人的立场和所知。purpose只描述此片段表达目的，"
        "不预写未经证实的答案、经历或现场细节。玩家完整输入是本场动机；当前片段执行导演分配目标，"
        "不独自完成整场请求，也不安排其他人的发言。already_completed_units是已经发生的交流。"
        "source_ids只选本角色给定观察事件，可为空；goal_id只选当前活跃目标，无对应目标填null。"
        "deception仅在角色设定或个人目标明确支持隐瞒、说谎时为true。"
        "retrieval=context表示工作集足以回应；需要未知往事或资料细节时选knowledge，speech=null。"
        "inspect必须经过真实查阅，不能用它模拟读过资料或执行物品操作。"
        "导演负责后续片段，remaining_request填空。不把下一名角色的回应写进本人的purpose或speech。"
        "仅向指定addressees中的一名主要接收者表达，不另向其他在场者提出待答问题；"
        "本项若允许提问，expects_reply=true且addressee_id仍是该指定对象。"
        "delivery_constraints禁止新问题时以回答、拒绝、未知或本人立场收束，不另安排一轮征询。"
        "只有确需玩家先行动、决定或授权才能继续时awaiting_player_action=true；"
        "普通回应或等待另一NPC接续为false。向玩家提问时wait_for_player=true，向NPC则false。"
        "含前置条件的世界动作不能提前执行；不得新增作者或玩家未设定的必经条件。"
        "trigger_reason简述当前事件为何推动本人回应；没有新机会且无待回应事项时可wait。"
    )


def draft_instruction(task, actor_id, info, *, segments=False):
    from .speech_packets import output_contract
    sources = {e["id"]: e["text"] for e in info.get("observations", [])}
    sources[f"profile:{actor_id}"] = json.dumps(info["profile"], ensure_ascii=False)
    sources.update({fact["event_id"]: fact["value"] for fact in info.get("scene_facts", {}).values()})
    return (
        performance_policy(task)
        + ("\n一次输出PacketTurnDraft：" if segments else "\n一次输出ActorTurnDraft：")
        + "intent填写本片段实际使用的决策字段；未使用的可选动作字段可省略。"
        "普通上下文发言明确填act=speak、retrieval=context；此时"
        "speech同时写出本片段台词。其他动作、wait或需要额外检索时speech=null，"
        "这些动作由运行时处理，不能预写成功回执。当前没有可检索文档；未知内容用人物的话说明未知。"
        "intent.purpose只描述表达目的，不能替speech预写事实答案；两者不是两段连续台词。"
        "若任务要你表达立场或回应问题，speech直接表达具体态度、回答、拒绝或本人当前未知。"
        "只说准备说明、让下一人说、将来再谈不完成这项回应；无需重复上一人的整段话。"
        "当前说不准原文不代表过去没看过，也不证明原文不存在、尚未收到或别人也不知道。"
        "意图接收者、是否等待答复与实际台词一致；导演目标不是既成事实或强制立场。"
        "任务addressees只有一人时，台词直接面向此人，不转头向玩家报告；可提及其他人的话。"
        "本片段只完成一次回应，遵守delivery_constraints的长度与节奏；收尾不额外索要新决定。"
        "演出交接由运行时处理，台词以人物在场的自然语言结束。"
        + output_contract(segments).replace("输出ActorSpeech JSON。", "speech采用ActorSpeech结构。")
        .replace("输出PacketSpeech JSON。", "speech采用PacketSpeech结构。")
        + ("联合输出的外层为PacketTurnDraft，" if segments else "联合输出的外层仍为ActorTurnDraft，")
        + "intent与speech是同一片段。"
        "\n可引用的本角色来源（数据）：" + json.dumps(sources, ensure_ascii=False)
    )
