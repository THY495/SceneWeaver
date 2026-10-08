"""Choose one NPC intention from that character's private working set."""

import asyncio
import json
import re

from ..context import ContextPressure
from ..observability.timing import annotate, measure, timed
from ..providers import ProviderError
from ..schemas import ActorIntent, ActorTurnDraft
from .context import NPCServices, TurnContext
from .item_expression import result_candidates, result_focus


@timed("npc.plan")
async def plan_actor(
    services: NPCServices,
    task,
    turn: TurnContext,
    actor,
    *,
    memory,
    replies,
    step: int,
    limit: int,
    last_actor: str | None,
    continuation: str,
    director_assignment: dict | None = None,
    operation_scope: str = "",
    combine_speech: bool = False,
) -> dict | None:
    annotate(actor_id=actor.id, step=step)
    db, runtime = services.db, services.runtime
    await memory.prepare_recall(task, actor.id)
    while True:
        try:
            planning, info = services.context.assemble(task, actor_id=actor.id)
            break
        except ContextPressure:
            runtime.check(task.id, task.lease_version)
            await asyncio.sleep(1)
    known_cast = turn.visible_cast(info)
    from .combined_generation import (
        basis_digest,
        draft_instruction,
        fallback_reason,
        joint_intent_instruction,
    )

    reason = fallback_reason(task, info, director_assignment) if combine_speech else "disabled"
    combined = combine_speech and reason is None
    from .speech_packets import PacketTurnDraft, to_actor_speech, uses_packets

    packets = uses_packets(services)
    draft_basis = basis_digest(info, planning) if combined else None
    annotate(actor_generation="combined_context" if combined else "separate", generation_fallback=reason)
    known_ids = {person["id"] for person in known_cast}
    from ..observations import observed_record

    with db.session() as s:
        completed_views = [observed_record(s, actor.id, r["event_id"]) for r in replies]
    planning = [
        *planning,
        {
            "role": "user",
            "content": "决策请求（数据）："
            + json.dumps(
                {
                    "trigger": "idle" if task.payload.get("autonomous") else "conversation",
                    "player_id": turn.player_id if turn.player_id in known_ids else None,
                    "scene_opportunity": task.payload.get("trigger")
                    if actor.id == task.payload.get("trigger_actor_id")
                    else None,
                    "participants": known_cast,
                    "step": step,
                    "current_player_message": None
                    if task.payload.get("autonomous") or turn.player_id not in known_ids
                    else continuation
                    if continuation and actor.id == last_actor
                    else task.payload["text"],
                    "already_completed_units": [
                        {"actor_id": r.actor_id, "text": r.text} for r in completed_views if r
                    ],
                    "continuation_only": bool(continuation and actor.id == last_actor),
                    "current_player_event_id": task.payload.get("event_id"),
                    "remaining": limit - step,
                },
                ensure_ascii=False,
            ),
        },
    ]
    original_system = planning[0]["content"]
    planning[0] = {
        "role": "system",
        "content": planning[0]["content"]
        + ("\n本次同时决定本片段意图并表达台词。" if combined else "\n本次只决定是否行动，不生成台词。")
        + "选择speak、wait或inspect实际检索自己的资料。"
        "根据未回答问题、承诺或目标提出一个具体意图；已经回答或无事可做就wait。"
        "conversation触发的第0片段优先处理current_player_message，不能因旧NPC问题尚未回答而忽略玩家新输入。"
        "后续片段才接续本轮已提交的交流。意图仅描述要做什么，不要预先编造未知答案或观察细节。"
        "复合请求若先执行一项，remaining_request写尚未完成的本轮要求，随后继续，不重复已完成项；无剩余则空。"
        "如果后续必须先由玩家行动（例如放下物品或补充资料），awaiting_player_action=true，"
        "本次说明所需行动后交还玩家，不把等待条件当成可以立即执行的剩余步骤；否则为false。"
        "每个片段只留下一个待答接收者和一个语义单元。若玩家只需简短确认、随后要NPC交换看法，"
        "可在同一片段简短回应玩家再向NPC提出唯一待答问题；不得同时要求玩家作新决定。"
        "玩家若提出需要实质回答的具体问题，则先回答、拒绝或承认未知，"
        "remaining_request记录后续要问NPC的问题；purpose只描述本片段，台词尽量简短。"
        "如果请求有如果/只有/等到等前置条件，未满足之前不能执行其依赖动作；独立请求可以分开处理。"
        "条件请求必须先确认条件成立，不能先执行物品动作。不得新增玩家未要求的工作。"
        "可主动追问另一NPC。问到玩家时wait_for_player=true；对NPC提问时false并填其ID。"
        "source_ids仅限给定观察事件。不要重复上轮内容。deception只有明确设定或已有个人目标支持才可true。"
        "若为已有目标行动则填goal_id，否则null。inspect只表示实际查阅自己的资料，不能移动物品或替玩家行动。"
        "retrieval=context仅当当前工作集足以回应普通接话、提问或明确给出的事实；"
        "涉及未知往事、缺失证据、资料细节时选knowledge；拿不准也选knowledge。"
        "trigger_reason说明新事件或尚未处理的问题如何推动目标；idle时没有新机会就wait，"
        "不能仅因目标仍未完成而重复催问。被回避的问题只有出现新证据或条件变化才再次追问。",
    }
    if combined:
        planning[0]["content"] = original_system + joint_intent_instruction()
    if director_assignment is not None:
        planning[0]["content"] += (
            "\n本轮有导演分配的单段任务：玩家完整请求是整场交流的动机，不要求你在此片段独自完成全部请求。"
            "本片段优先判断导演任务是否符合自身立场；可同意、拒绝、暂缓或wait。"
            "目标要求回答玩家具体问题时，purpose必须保留该问题，不能缩成复述玩家意愿。"
            "不知答案可以明确说明未知或需要核实，不得编造。收尾回应不额外索要新的决定或资料。"
            "任务指定先与NPC商量时，应面向该NPC，可简短确认玩家请求，"
            "也不把稍后向玩家汇报并进同一purpose。向NPC征询意见不等于等待玩家。"
            "导演任务的addressees若有一人，该ID是本片段唯一主要接收者；"
            "其他在场角色仍可听见，无需把公开发言改成对玩家报告。"
            "后续演出由导演安排，本次remaining_request填空，不接管其他片段的待办。"
            "dialogue_capacity表示本段可安排的回应位置，不是人物知识或行动结果。"
            "向NPC提出待答问题时，优先选scheduled_reply_actor_ids中的对象；can_schedule_more=false且对象没有后续位置时，"
            "以已有答案、未知、拒绝或留待后续的意愿收束，不附加需要该NPC立刻回复的新问题。"
            "already_completed_units是本轮已经正式发生的片段，不要把它们再次写入purpose。"
            "若当前任务仅询问玩家，purpose只描述询问；前一段回应NPC已完成，不能再以‘回应后询问’重做。"
        )
        planning.append(
            {
                "role": "user",
                "content": "导演分配的演出目标（不是事实，不强迫同意或泄密）："
                + json.dumps(director_assignment, ensure_ascii=False)
                + "\n在自身知情和动机范围内回应此目标；可以拒绝、暂缓或等待玩家。",
            }
        )
    object_instruction = (
        "\nknown_objects是已登记可操作物品，不能凭空登记物品或宣布未记录的行动成功。"
        "想在当前可观察范围寻找已知物品时act=search，search_action填object_id；"
        "记忆中的旧位置可以支持尝试，但不保证找到，不能凭空获知远处位置。"
        "known_agreements只包含你实际观察到的提案版本、同意范围和完成记录。"
        "如需建立有限的条件行动计划，act=agreement_proposal，agreement_proposal填写"
        "source_event_id（自己已观察的发言）、items（最多8个，key唯一，executor_id、action、depends_on和conditions）。"
        "source_event_id仅为动机来源，不代表原发言者已同意；不能捏造已发生行动。"
        "新提案expected_version=0，不填proposal_id；修改自己的未执行提案须填当前ID和版本。"
        "不要重复建立内容相同的未完成提案；提案本身不等于任何人已同意。"
        "若决定同意、拒绝或撤回其中的事项，act=agreement_decision，"
        "填写agreement_decision的proposal_id、expected_version、item_ids、decision。"
        "只能为自己决定，普通answer不等于协议同意；无需为了推进强行同意。"
        "执行已同意的事项时object_action.agreement填proposal_id、version、item_id，"
        "物品expected_version使用最新观察到的版本，不能跳过依赖事项。"
        "实际拿取/放下/交给同场NPC时act=object_action，填object_action的object_id、expected_version、"
        "verb=take/put/give及target_id。不能取别人持有的物品，不能替玩家接受交接；可放下等玩家拿取。"
        "known_objects中protocol=2的物品必须用act=item_action，填写item_action：item_id、"
        "expected_revision（该物品version）、action=move/open/close。move另填destination的kind="
        "actor/location/container及target；拿取目标为自己角色ID，放下为当前地点，放入为已知容器ID。"
        "新版物品递交用item_action：action=give，destination为actor及接收者ID，agreement填已获双方同意的proposal_id、version、item_id。"
        "协议事项action仍用give描述意图，但实际执行不能用旧版object_action。接收提案时先agreement_decision，自愿接受或拒绝；普通口头同意不算授权。"
        "不得猜测隐藏物品ID；新物品暂不支持解锁和销毁。规则拒绝时不宣称成功。"
        "自己的已记录item_action_result为rejected时，若目标和相关条件没有变化，不重复同一失败动作。"
        "优先用speak向在场请求者简短说明实际阻碍，或提出可执行的替代方案；无对象需要解释则wait。"
        "解释依据是已提交的失败记录，不将失败写成完成；成功打开后只依据新观察到的内容继续操作。"
        "若本片段仅简短说明某次已提交物品操作的结果，act=speak并将result_event_id填该观察事件ID。"
        "这不是执行动作，只解释那次结果；复杂历史问题或另提新要求不填此字段。"
    )
    planning[0]["content"] += (
        object_instruction if not combined or info.get("known_objects") or info.get("known_agreements")
        else "\n当前工作集没有已登记可操作物品或协议；本片段在实际见闻范围内选择表达或等待。"
        "若要发起世界动作，仍用相应动作字段交给运行时，speech=null；不要猜测物品ID或预写成功。"
    )
    planning[0]["content"] += (
        "有pending_replies时可填reply_to对应question_id，并选择response_kind=answer/refuse/defer/silence。"
        "不知道可以回答不知道，拒答须有角色动机，暂缓须明确说明；有待回应问题不应无故wait。"
        "当前玩家输入优先，旧问题可以暂缓。向别人提问时expects_reply=true并指定addressee_id。"
    )
    handoff = None
    if task.payload.get("handoff_proposal_id"):
        handoff = next(
            (
                proposal
                for proposal in info.get("known_agreements", [])
                if proposal["proposal_id"] == task.payload["handoff_proposal_id"]
                and any(
                    item["id"] == task.payload.get("handoff_item_id")
                    and item["action"]["verb"] == "give"
                    and item["action"]["target_id"] == actor.id
                    and actor.id not in proposal["decisions"].get(item["id"], {})
                    for item in proposal["items"]
                )
            ),
            None,
        )
        if handoff is None:
            raise ProviderError("递交提议已变化或接收角色无法观察，不能代为决定。")
        planning[0]["content"] += (
            "\n当前是玩家明确向你递交物品的专门回合。只根据你的角色动机决定接受或拒绝；"
            "必须使用act=agreement_decision，decision只能是accept或refuse。"
            "不能只口头表示接受/拒绝，也不能转为公告讨论、其它行动或wait。"
            "决定后玩家仍需亲自完成实际递交。不要为了剧情强制接受。"
        )
        planning.append(
            {
                "role": "user",
                "content": "本次待决事项（来自你已观察的协议，不是额外事实）："
                + json.dumps(
                    {
                        "proposal_id": handoff["proposal_id"],
                        "expected_version": handoff["version"],
                        "item_ids": [task.payload["handoff_item_id"]],
                        "decision_choices": ["accept", "refuse"],
                    },
                    ensure_ascii=False,
                ),
            }
        )
    if task.payload.get("item_result_event_id"):
        planning[0]["content"] += (
            "\n当前玩家已经完成一次物品交接。你是被指定的接收者；本轮只回应已提交的结果，"
            "使用act=speak并引用item_result_event_id，不再重复作同意决定或执行物品动作。"
        )
    candidates = result_candidates(info)
    if candidates:
        planning.append(
            {
                "role": "user",
                "content": "结果说明路由（数据）："
                + json.dumps(candidates, ensure_ascii=False)
                + "\n请明确输出result_event_id。仅解释上述一次操作的结果或直接阻碍时选对应ID；"
                "若需要比较多次操作、推断历史原因、制定方案或同时处理新要求，则填null，走完整回答。"
                "不能为了选择简短说明而丢弃问题的一部分。speak之外必须填null。",
            }
        )
    valid = {e["id"] for e in info.get("observations", [])}
    valid.update(f["event_id"] for f in info.get("scene_facts", {}).values())
    reply_ids = {q["question_id"] for q in info.get("pending_replies", [])}
    goal_ids = set(info.get("active_goals", {}))
    # Preserve the existing unassigned-public-input normalization below.
    from ..storage import SceneRecord

    with db.session() as s:
        current_event = s.get(SceneRecord, task.payload["event_id"]) if task.payload.get("event_id") else None
        if current_event and not current_event.payload.get("exchange", {}).get("expects_reply"):
            reply_ids.add(current_event.id)
    if combined:
        planning[0]["content"] += "\n" + draft_instruction(task, actor.id, info, segments=packets)
    failures = []
    for attempt in range(2):
        request = (
            planning
            if not attempt
            else [
                *planning,
                {
                    "role": "user",
                    "content": "上次意图中的引用未通过检查。source_ids只能逐字选可用来源，可为空。"
                    "reply_to只能选本角色仍待回应的question_id；不要用自己发出的提问、"
                    "已结束事项或普通观察ID代替。没有对应待办时可用null进行普通交流；"
                    "goal_id只能选当前活跃目标；目标已结束或不确定时填null。"
                    "result_event_id只能选给定结果ID且act=speak，否则填null；复杂或复合问题也填null。"
                    "item_protocol_mismatch表示把新版物品误当成旧版：对protocol=2必须使用item_action，"
                    "不能使用object_action；拿取用move且destination为actor和自己的ID。"
                    "handoff_decision_required表示本轮是待决递交，必须对指定提案选择accept或refuse；"
                    "handoff_result_speech_required表示实际交接已完成，只需简短回应结果。"
                    "director_addressee_mismatch表示本片段发言的主要接收者与导演单人任务不同；"
                    "可以在发言中简短提到其他在场人，但addressee_id须填指定角色。"
                    "不要凭空补建义务。重新输出完整意图；使用ActorTurnDraft时同时保留speech，"
                    "speech_required表示context发言必须提供台词："
                    + json.dumps(
                        {
                            "issues": failures,
                            "source_ids": sorted(valid),
                            "reply_to": sorted(reply_ids),
                            "goal_ids": sorted(goal_ids),
                            "result_event_ids": [event["id"] for event in candidates],
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
        )
        generated = await services.provider.chat(
            task.id,
            "dialogue" if combined else "role_intent",
            request,
            schema=(PacketTurnDraft if packets else ActorTurnDraft) if combined else ActorIntent,
            max_tokens=2200 if combined else 900,
            operation=(
                f"{operation_scope}actor-turn-v3:{step}:{actor.id}:{basis_digest(info, request)[:24]}:{attempt}"
                if combined
                else f"{operation_scope}intent:{step}:{actor.id}:{attempt}"
            ),
        )
        if combined:
            candidate = (PacketTurnDraft if packets else ActorTurnDraft).model_validate(generated)
            intent = candidate.intent.model_dump()
            draft = (to_actor_speech(candidate.speech.model_dump()) if packets
                     else candidate.speech.model_dump()) if candidate.speech else None
        else:
            intent, draft = generated, None
        if task.payload.get("item_result_event_id") and intent.get("act") == "speak":
            # This system-triggered turn only explains the committed player handoff.
            # result_focus below still requires this actor's actual observation.
            intent["result_event_id"] = task.payload["item_result_event_id"]
        with measure("npc.intent_validation") as validation:
            failures = []
            if combined and intent["act"] == "speak" and intent["retrieval"] == "context" and draft is None:
                failures.append("speech_required")
            if not set(intent["source_ids"]).issubset(valid):
                failures.append("source_unavailable")
            if intent.get("reply_to") not in {None, *reply_ids}:
                failures.append("reply_unavailable")
            if intent.get("goal_id") not in {None, *goal_ids}:
                failures.append("goal_unavailable")
            assigned_to = (director_assignment or {}).get("addressees") or []
            if (
                len(assigned_to) == 1
                and intent.get("act") == "speak"
                and intent.get("addressee_id") != assigned_to[0]
            ):
                failures.append("director_addressee_mismatch")
            if intent.get("act") == "object_action" and any(
                item.get("protocol") == 2
                and item["id"] == (intent.get("object_action") or {}).get("object_id")
                for item in info.get("known_objects", [])
            ):
                failures.append("item_protocol_mismatch")
            if handoff:
                decision = intent.get("agreement_decision") or {}
                if (
                    intent.get("act") != "agreement_decision"
                    or decision.get("proposal_id") != handoff["proposal_id"]
                    or decision.get("expected_version") != handoff["version"]
                    or decision.get("item_ids") != [task.payload["handoff_item_id"]]
                    or decision.get("decision") not in {"accept", "refuse"}
                ):
                    failures.append("handoff_decision_required")
            if task.payload.get("item_result_event_id") and intent.get("act") != "speak":
                failures.append("handoff_result_speech_required")
            try:
                result_focus(intent, info)
            except ProviderError:
                failures.append("result_focus_invalid")
            if failures == ["reply_unavailable"] and intent.get("addressee_id") in known_ids:
                from .intent_reference import normalize_statement_reference

                normalized = normalize_statement_reference(intent, info)
                if normalized is not intent:
                    intent = normalized
                    failures = []
                    validation.mark("ordinary_statement_reference_normalized")
            for code in failures:
                validation.mark(code)
            validation.mark("rejected" if failures else "accepted")
        if not failures:
            break
        if attempt:
            if "handoff_decision_required" in failures:
                raise ProviderError("接收角色未对递交提议作出明确决定，物品仍由玩家持有。")
            if "handoff_result_speech_required" in failures:
                raise ProviderError("接收角色未能回应已完成的物品交接。")
            if "item_protocol_mismatch" in failures:
                raise ProviderError("新版物品意图修正后仍使用旧版操作，未执行动作。")
            if "result_focus_invalid" in failures:
                raise ProviderError("结果说明必须是发言且引用本角色已观察到的物品结果。")
            if "source_unavailable" in failures:
                raise ProviderError("角色意图引用了未感知的事件。")
            if "goal_unavailable" in failures:
                raise ProviderError("意图引用的目标不属于此角色的活跃目标。")
            if "director_addressee_mismatch" in failures:
                raise ProviderError("演员意图未面向导演指定的主要接收者。")
            if "speech_required" in failures:
                raise ProviderError("合并发言缺少台词，未提交。")
            raise ProviderError("待回应事项不属于本角色或已结束。")
    if intent.get("addressee_id") not in {None, *known_ids}:
        raise ProviderError("意图对象不在当前交流范围。")
    # A concrete addressee determines who has the next turn; do not trust conflicting flags.
    if intent.get("addressee_id"):
        intent["wait_for_player"] = intent["addressee_id"] == turn.player_id
    current_questions = [
        q
        for q in info.get("pending_replies", [])
        if q["task_id"] == task.id or q["question_id"] == task.payload.get("resume_event_id")
    ]
    if current_questions and not intent.get("reply_to"):
        intent["reply_to"] = current_questions[-1]["question_id"]
    if intent.get("reply_to") == task.payload.get("event_id") and not current_questions:
        # Public input without an explicit recipient is a valid conversational source,
        # but it does not establish a private/assigned obligation to resolve.
        from ..storage import SceneRecord

        with db.session() as s:
            source_event = (
                s.get(SceneRecord, task.payload["event_id"]) if task.payload.get("event_id") else None
            )
            if source_event and not source_event.payload.get("exchange", {}).get("expects_reply"):
                intent["reply_to"] = None
    if intent.get("reply_to") and intent["act"] == "wait":
        intent.update(
            act="speak",
            response_kind="silence",
            expects_reply=False,
            addressee_id=turn.player_id,
            wait_for_player=True,
        )
    if intent.get("reply_to") not in {
        None,
        *(q["question_id"] for q in info.get("pending_replies", [])),
    }:
        raise ProviderError("待回应事项不属于本角色或已结束。")
    if intent["act"] == "object_action" and not intent.get("object_action"):
        raise ProviderError("物品行动缺少明确参数，未执行。")
    if intent["act"] == "item_action" and not intent.get("item_action"):
        raise ProviderError("新版物品行动缺少明确参数，未执行。")
    goal = info.get("active_goals", {}).get(intent.get("goal_id"), "")
    if intent["deception"] and not re.search("隐瞒|说谎|欺骗|掩饰", info["profile"]["core"] + goal):
        raise ProviderError("角色没有明确的欺骗动机设定。")
    if intent["act"] == "wait":
        return None
    if intent["act"] == "inspect" and not info["profile"]["document_ids"]:
        intent = {
            **intent,
            "act": "speak",
            "purpose": "说明自己没有可查资料，征求补充",
            "wait_for_player": True,
        }
    if director_assignment is not None:
        # The director owns subsequent jobs. The legacy actor continuation field
        # must not turn a later actor's assignment into this candidate's obligation.
        intent["remaining_request"] = ""
    if combined:
        if (
            draft is not None
            and candidate.intent.act == "speak"
            and intent["act"] == "speak"
            and intent["retrieval"] == "context"
            and intent.get("response_kind") != "silence"
        ):
            intent["_speech_draft"] = {
                "speech": draft,
                "actor_revision": info["actor_revision"],
                "basis_digest": draft_basis,
            }
        else:
            annotate(generation_fallback="action_or_knowledge", combined_speech_reused=False)
    return intent
