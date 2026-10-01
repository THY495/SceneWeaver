"""Prepare one actor response or action; all world commits stay in the coordinator."""

import asyncio
import json
import re
from functools import partial

from ..context import ContextPressure
from ..evidence import model_evidence, normalize_citations
from ..observability.timing import annotate, measure, timed
from ..providers import OutputTruncated, ProviderError, estimate_tokens
from ..role_policy import (
    ROLE_EXPRESSION_POLICY,
    citation_repair_sources,
    claim_issues,
    director_handoff_leak,
    needs_knowledge,
    semantic_review_marks,
    unexecuted_stage_object_action,
)
from ..schemas import ActorReview, ActorSpeech
from ..storage import Conversation, Task
from .audit import retrieval_message
from .context import NPCServices, TurnContext
from .item_expression import (
    RESULT_EXPRESSION,
    RESULT_LANGUAGE,
    bind_result_quotes,
    expression_token_budget,
    result_candidates,
    result_focus,
)
from .review import review_messages
from .speech_projection import project_speech


class UnsafeDraftExhausted(ProviderError):
    """All bounded speech candidates were rejected before publication."""


@timed("npc.perform")
async def perform_actor(
    services: NPCServices,
    task: Task,
    turn: TurnContext,
    actor: Conversation,
    intent: dict,
    step: int,
    *,
    operation_scope: str = "",
    voice_service=None,
    voice: str = "",
    director_assignment: dict | None = None,
):
    annotate(actor_id=actor.id, step=step, voice_candidate_enabled=voice_service is not None)
    db, runtime = services.db, services.runtime
    while True:
        runtime.check(task.id, task.lease_version)
        try:
            # Speech adds policy, schema and one bounded repair to the memory set.
            # On smaller configured windows retain at least the legacy workset
            # allowance; the provider still checks the complete request boundary.
            reserve = max(6000, min(10000, services.config.context_hard_tokens // 2))
            context, info = services.context.assemble(task, actor_id=actor.id, reserve_tokens=reserve)
            break
        except ContextPressure:
            await asyncio.sleep(1)
    known_cast = turn.visible_cast(info)
    known_ids = {person["id"] for person in known_cast}
    focused_result = result_focus(intent, info)
    if intent.get("act") == "agreement_proposal":
        from ..agreements import prepare_proposal
        from ..schemas import ProposalInput

        body = ProposalInput.model_validate(
            {**(intent.get("agreement_proposal") or {}), "request_id": f"{task.id}:{step}"}
        )
        with db.session() as s:
            answer, _, _, _ = prepare_proposal(s, actor, body, f"task:{task.id}:{step}")
        return {
            "id": actor.id,
            "name": info["profile"]["name"],
            "revision": info["actor_revision"],
            "answer": answer,
            "spoken_parts": [answer],
            "evidence": [],
            "missing": [],
        }
    if intent.get("act") == "search":
        from ..scene_state import search_object

        with db.session() as s:
            _, answer, _, _ = search_object(s, actor, (intent.get("search_action") or {}).get("object_id"))
        return {
            "id": actor.id,
            "name": info["profile"]["name"],
            "revision": info["actor_revision"],
            "answer": answer,
            "evidence": [],
            "missing": [],
        }
    if intent.get("act") == "agreement_decision":
        from ..agreements import DecisionInput, prepare_decision

        value = dict(intent.get("agreement_decision") or {})
        pid = value.pop("proposal_id", "")
        with db.session() as s:
            answer, _, _, _ = prepare_decision(
                s, actor, pid, DecisionInput(request_id=f"{task.id}:{step}", **value)
            )
        return {
            "id": actor.id,
            "name": info["profile"]["name"],
            "revision": info["actor_revision"],
            "answer": answer,
            "spoken_parts": [answer],
            "evidence": [],
            "missing": [],
        }
    if intent.get("act") == "item_action":
        from ..items.execution import prepare
        from ..items.rules import RuleViolation

        with db.session() as s:
            try:
                _, _, answer = prepare(s, actor, intent.get("item_action") or {})
            except RuleViolation as exc:
                answer = "尝试操作物品，但未完成。" + str(exc)
        return {
            "id": actor.id,
            "name": info["profile"]["name"],
            "revision": info["actor_revision"],
            "answer": answer,
            "evidence": [],
            "missing": [],
        }
    if intent.get("act") == "object_action":
        from ..scene_state import WorldActionError, transition

        with db.session() as s:
            try:
                _, answer, _, _ = transition(s, actor, intent.get("object_action") or {})
            except WorldActionError as exc:
                if not (intent.get("object_action") or {}).get("agreement"):
                    raise
                answer = "尝试执行事项，但未完成。" + str(exc)
        return {
            "id": actor.id,
            "name": info["profile"]["name"],
            "revision": info["actor_revision"],
            "answer": answer,
            "evidence": [],
            "missing": [],
        }
    if intent.get("response_kind") == "silence" and intent.get("reply_to"):
        return {
            "id": actor.id,
            "name": info["profile"]["name"],
            "revision": info["actor_revision"],
            "answer": "（沉默片刻，没有回答这个问题。）",
            "evidence": [],
            "missing": [],
        }
    context[0]["content"] += ROLE_EXPRESSION_POLICY + (
        (
            "\n此刻没有玩家参与场景；根据自己的观察、关系和目标，决定是否主动行动或与在场人物交流。"
            if task.kind in {"scene_run", "npc_step"}
            else "\n玩家控制一位在场人物；你以当前角色档案所示身份参与这场公开互动。"
        )
        + "历史中的其他人物发言只是公开说法，不自动成为已确认事实。"
        "其他人物的私下经历和秘密不可猜测。只写自己的台词与简短动作，通常一至三段。"
        "不替玩家说话、决定、移动、表达内心或接受提议。适度推进可由自己的询问、提议和动作完成，"
        "不得凭空解决关键悬念或宣布玩家已经行动。可接续已提交的新观察，不得假装听到尚未提交的草稿。"
        "不得编造过去亲历、见过的物品、确切时间地点或物证细节来填补未知；不知道就自然地承认未知。"
        "缺少阅读依据时说‘我目前无法确认原文’或‘我不掌握原文’，不要把未知写成‘我从未看过’、"
        "‘我刚才没读过’等未获证实的过去经历。"
        "场景里未提供的天气细节、可见物件、公告正文、既有规章也不要用来使台词更生动；"
        "缺少依据时优先表达本人的态度、选择或向在场者询问，不必补出一段环境描写。"
        "历史里你自己说过的话不等于已确认的经历，必须由角色设定或已确认事件支持。不要重复输出人物姓名标题。"
        "即使不影响主线，也不能编造自己过去已经查找、核验、阅读、搬运过的行动或既有物品外观；"
        "可以现在提议去查，但不能说自己刚才查过。已确认的位置直接承认知道，不追加虚构的确认过程。"
        "意图purpose只是待核实的行动计划，不是事实来源；其中无依据的具体细节必须舍弃。"
        "已登记物品操作只能由object_action或item_action执行，普通台词不能宣称完成新操作。"
        "可依据自己实际观察到的item_action_result解释此前成功或失败；rejected仅证明尝试未完成。"
        "规则返回locked只证明当次上锁阻碍，不能据此编造锁扣、钥匙孔、密码盘等具体外观或解锁机制。"
        "所有权或口头允许不等于锁已解除，不承诺仅确认所有人即可重试成功。失败解释优先一句说明加一个必要询问。"
        "known_objects是最近实际观察到的状态，不证明未观察期间始终未变。"
        "response_kind=refuse时明确以角色身份拒绝；defer则明确暂缓，不伪装已回答。"
        "只实现本片段purpose，面向addressee_id，不要提前说remaining_request里留给下一片段的话。"
        "即使玩家同时提出多项要求，本片段也不能越过意图边界。正文直接进入实质内容，不重复接话开场。"
    )
    context.append(
        {"role": "user", "content": "获准的本次意图（数据）：" + json.dumps(intent, ensure_ascii=False)}
    )
    if director_assignment is not None:
        context.append(
            {
                "role": "user",
                "content": "已通过本角色知情检查的单段演出任务（数据，不是事实或强制立场）："
                + json.dumps(director_assignment, ensure_ascii=False)
                + "\n结合自身意图，仅完成本片段的一次回应，面向一个主要接收者。"
                "遵守delivery_constraints中的表达长度与节奏；不要把这些后台要求念给角色听。"
                "演出交接由后台处理；台词只表达人物此刻的态度和意图，并以场景内的自然语言收尾。"
                "回答任务中的具体问题，不知答案时明确说明未知或需核实，不能只复述对方的意愿。"
                "完成确认或收尾就自然结束，不习惯性附加新问题；只有本片段目的确实需要回答时才提问。"
                "整场玩家请求可由后续角色/片段完成；向另一人物商量前可简短回应玩家，"
                "但只向本片段对象提出一个待答请求，不再转头要求玩家回答。"
                "提及他人不等于对他人发言，但不要增加要求另一人答复或行动的第二个交付目标。"
                "若不同意目标，可向本片段对象表达拒绝或保留，不要为了推进编造事实。",
            }
        )
    context.append(
        {
            "role": "user",
            "content": "本轮触发（数据）："
            + json.dumps(
                {
                    "player_message": None
                    if task.payload.get("autonomous") or turn.player_id not in known_ids
                    else task.payload["text"],
                    "event_id": task.payload.get("event_id"),
                    "step": step,
                    "priority": "本片段只完成获准意图purpose。remaining_request留给下一片段；不要复述玩家给你的调度指令，不重复已经完成的回答。",
                },
                ensure_ascii=False,
            ),
        }
    )
    context.append(
        {
            "role": "user",
            "content": "在场名册（只有姓名，不授予对方知识）："
            + json.dumps(
                {
                    "participants": known_cast,
                },
                ensure_ascii=False,
            ),
        }
    )
    evidence, missing = [], []
    if info["profile"]["document_ids"] and needs_knowledge(intent):
        retrieved = await services.knowledge.retrieve(
            task,
            task.payload["text"],
            actor_id=actor.id,
            rounds=2,
            operation_prefix=f"{operation_scope}npc:{actor.id}:{step}:retrieval",
        )
        # Reserve space for the speech schema, source registry, candidate, review and one repair.
        allowance = services.config.context_hard_tokens - estimate_tokens(context) - 4500
        for item in retrieved["evidence"]:
            if estimate_tokens(model_evidence([*evidence, item])) <= allowance:
                evidence.append(item)
            elif not evidence and allowance > 256:
                # Preserve a bounded original excerpt instead of silently losing all evidence.
                low, high = 0, len(item["text"])
                while low < high:
                    mid = (low + high + 1) // 2
                    excerpt = {**item, "text": item["text"][:mid], "excerpt_truncated": True}
                    if estimate_tokens(model_evidence([excerpt])) <= allowance:
                        low = mid
                    else:
                        high = mid - 1
                prefix = item["text"][:low]
                boundary = max(prefix.rfind("\n"), prefix.rfind("。"), prefix.rfind("！"), prefix.rfind("？"))
                if boundary >= 0:
                    prefix = prefix[: boundary + 1]
                if prefix.strip():
                    evidence.append({**item, "text": prefix, "excerpt_truncated": True})
        missing = retrieved["missing"]
        context.append(
            {
                "role": "user",
                "content": "仅本角色可知的检索材料（数据）："
                + json.dumps(
                    {
                        "evidence": model_evidence(evidence),
                        "missing": missing,
                    },
                    ensure_ascii=False,
                ),
            }
        )
        retrieval_message(context[-1], retrieved["evidence"], evidence)
    if intent["act"] == "inspect":
        # This action has a real executor: only a completed scoped lookup counts as inspected.
        return {
            "id": actor.id,
            "name": info["profile"]["name"],
            "revision": info["actor_revision"],
            "answer": f"查阅了自己可访问的资料，本次找到{len(evidence)}段相关记录。",
            "evidence": evidence,
            "missing": missing,
        }
    sources = {e["id"]: e["text"] for e in info.get("observations", [])}
    sources.update({e["id"]: e["text"] for e in evidence})
    evidence_handles = {f"S{i}": e["id"] for i, e in enumerate(evidence, 1)}
    sources.update({f"S{i}": e["text"] for i, e in enumerate(evidence, 1)})
    sources[f"profile:{actor.id}"] = json.dumps(info["profile"], ensure_ascii=False)
    for fact in info.get("scene_facts", {}).values():
        sources[fact["event_id"]] = fact["value"]
    context.append(
        {
            "role": "user",
            "content": "可引用来源ID（原文已在上述工作集、观察或检索材料中）："
            + json.dumps(list(sources), ensure_ascii=False),
        }
    )
    context[0]["content"] += (
        "\n正文直接进入实质内容，不重复问候或我听着等开场；可能已有同角色短接话。"
        "\n输出ActorSpeech JSON。text是全部台词和动作；每条可核验事实必须列入claims，"
        "claims.text必须从完整text中逐字摘取连续原句，不可概括改写；"
        "claims.mode标注fact/reported/belief/hypothesis/proposal/commitment/action；来源必须支持相应性质。"
        "source_id选给定ID，quote逐字摘自该来源原文。纯提问、建议、表情可以不列claims；不得伪造来源或引文。"
        "引用speech来源只可用于reported等发言/主观记录，不能把它的内容标为fact或已执行action；"
        "回忆自己或别人说过、答应过的话也用reported，不将台词中的经历当作独立证明。"
        "玩家说自己听见某句话，不等于你也听见；若没有本角色的观察，不能说‘我也听到’。"
        "自己先前说过‘我没听见’也只证明自己说过，不独立证明现在的听闻经历；"
        "可说‘我之前说过没听见，但目前不能替你确认’，不要把旧自述重新标为事实。"
        "获准的隐瞒或欺骗可作为角色行为，但不把谎言标为有据事实。"
        "个人目标不等于双方已达成约定：只有原始观察明确记载双方同意，才能说我们说好、已经约定；"
        "否则说我希望或我提议。被问到已获知的具体内容时直接回答，不用那两句、那件事代替内容。"
        "spoken_parts按顺序列出text中可直接朗读的台词，每项必须逐字来自text；"
        "不得包含括号动作、旁白、角色姓名标题、语音控制标签。没有台词则空列表。"
        "performance与spoken_parts逐项对应，text完全相同；expression选表情枚举，pose选原地姿态枚举，"
        "voice_direction仅写音量、语速、情绪等简短语气描述。不得借表现字段加入秘密或执行世界动作。"
    )
    if result_candidates(info):
        context[0]["content"] += "\n" + RESULT_LANGUAGE
    if focused_result:
        context.append(
            {
                "role": "user",
                "content": RESULT_EXPRESSION
                + json.dumps(
                    {
                        "event_id": focused_result["id"],
                        "text": focused_result["text"],
                        "result": focused_result["detail"]["item_action_result"],
                    },
                    ensure_ascii=False,
                ),
            }
        )
    annotate(
        item_result_expression=bool(focused_result),
        result_event_id=focused_result["id"] if focused_result else None,
    )
    draft_context = list(context)
    citation_repairs = semantic_repairs = 0
    private_audio, audio_status = None, "not_requested"
    # One local citation repair and up to two semantic repairs may be needed
    # for the same candidate. Every revision still passes both reviews before
    # publication; this is a bounded recovery path, not a review bypass.
    for attempt in range(4):
        output_tokens = expression_token_budget(focused_result, info)
        try:
            speech = await services.provider.chat(
                task.id,
                "dialogue",
                draft_context,
                schema=ActorSpeech,
                max_tokens=output_tokens,
                operation=f"{operation_scope}npc:{actor.id}:{step}:answer:{attempt}",
            )
        except OutputTruncated:
            # A truncated schema cannot be reviewed or repaired in place. Retry
            # only this actor candidate once with more output room.
            if output_tokens >= 2400:
                raise
            with measure("npc.output_limit_retry"):
                annotate(candidate_revision=attempt)
                speech = await services.provider.chat(
                    task.id,
                    "dialogue",
                    draft_context,
                    schema=ActorSpeech,
                    max_tokens=min(2400, output_tokens + 1200),
                    operation=f"{operation_scope}npc:{actor.id}:{step}:answer:{attempt}:expanded",
                )
        speech = bind_result_quotes(speech, focused_result)
        answer = speech["text"]
        with measure("npc.claim_validation") as validation:
            annotate(candidate_revision=attempt)
            try:
                # Text-level handles can be present even when claims is empty. Validate them
                # inside the repair loop, before semantic review and publication.
                normalize_citations(answer, evidence)
                invalid = claim_issues(
                    speech, sources, info.get("observations", []), task.payload.get("text", "")
                )
                if director_assignment is not None and director_handoff_leak(answer):
                    invalid.append({"code": "director_control_leak"})
                if unexecuted_stage_object_action(answer):
                    invalid.append({"code": "unexecuted_stage_object_action"})
            except ProviderError:
                invalid = [{"code": "citation_handle_invalid"}]
            for code in sorted({issue["code"] for issue in invalid}):
                validation.mark(code)
            annotate(local_issue_count=len(invalid))
            validation.mark("rejected" if invalid else "accepted")
        if invalid:
            if citation_repairs:
                raise UnsafeDraftExhausted(
                    "声明、来源或后台控制校验未通过（"
                    + ", ".join(sorted({item["code"] for item in invalid}))
                    + "），本片段未提交。"
                )
            citation_repairs += 1
            draft_context = [
                *context,
                {
                    "role": "user",
                    "content": "上次本地校验未通过。若invalid_entries含director_control_leak，"
                    "请删去场景/回合/控制权交还等后台调度用语，只保留角色在世界中的自然回应。"
                    "若含unexecuted_stage_object_action，删除括号里未经世界动作执行的物品操作；"
                    "可保留点头、停顿等不改变物品状态的表演，不能改写成另一种物品动作。"
                    "claims.text必须逐字出现在最终台词text中；"
                    "quote必须逐字出现在对应来源。台词中用引号引用历史原话也必须逐字对应原始来源；"
                    "若只是概括就不要伪装成直接引文。speech来源只能证明曾经说过，"
                    "不能用mode=fact/action证明话中内容；转述使用reported并在台词明确归属。"
                    "玩家听闻不能变成你自己的听闻；你自己的旧说法也不能证明现在确实如此。"
                    "没有独立证据的事实应承认不知道，不重复断言。"
                    "未知原文可说‘目前无法确认’，不要改写成无据的‘我从未看过’。"
                    "重新写出台词与claims，删除无据细节。"
                    "source_texts_for_repair仅重复已授权来源正文；quote逐字摘自其中text，"
                    "不要引用观察的包装说明、说话者标签或自己改写的概括。"
                    "正文中的[S编号]只能引用实际提供的检索材料；没有对应材料就删除该编号。"
                    + json.dumps(
                        {
                            "rejected_candidate": {"text": speech["text"], "claims": speech["claims"]},
                            "invalid_entries": invalid,
                            "source_texts_for_repair": citation_repair_sources(speech, sources, invalid),
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
            continue
        speech, projection_failed = await project_speech(
            services.provider,
            task.id,
            speech,
            operation=f"{operation_scope}npc:{actor.id}:{step}:speech_projection:{attempt}",
            repair=voice_service is not None,
        )
        review_call = partial(
            services.provider.chat,
            task.id,
            "actor_review",
            review_messages(
                info,
                model_evidence(evidence),
                speech,
                sources,
                actor_id=actor.id,
                documents=evidence,
                intent=intent,
                player_request=task.payload.get("text", "") if turn.player_id in known_ids else "",
                cast=known_cast,
            ),
            schema=ActorReview,
            max_tokens=1000,
            operation=f"{operation_scope}npc:{actor.id}:{step}:review:{attempt}",
        )
        if voice_service is None:
            review = await review_call()
        else:
            from ..performance_candidate import CandidateIdentity
            from ..voice import digest
            from ..voice_candidate import first_sentence
            from .review_audio import reviewed_audio

            first = first_sentence(answer, speech.get("spoken_parts", []))
            direction = next(
                (
                    p.get("voice_direction", "")
                    for p in speech.get("performance", [])
                    if first and first in p.get("text", "")
                ),
                "",
            )

            def is_current():
                runtime.check(task.id, task.lease_version)
                with db.session() as s:
                    live = s.get(Conversation, actor.id)
                    return live is not None and live.revision == info["actor_revision"]

            identity = CandidateIdentity(
                task.session_id or "",
                task.epoch,
                operation_scope,
                task.lease_version,
                f"{actor.id}:{step}",
                attempt,
                digest(answer),
                voice,
            )
            review, private_audio, audio_status = await reviewed_audio(
                identity,
                review_call,
                (lambda: voice_service.stream_candidate(task, first, voice, direction)) if first else None,
                is_current,
                discard_audio=voice_service.discard_candidate,
            )
            if projection_failed:
                audio_status = "failed"
                annotate(audio_status=audio_status, projection_status="failed")
        with measure("npc.semantic_review_result") as validation:
            annotate(
                candidate_revision=attempt,
                review_accepted=review["accepted"],
                review_issue_count=len(review.get("issues", [])),
                review_unsupported_count=sum(not check["supported"] for check in review.get("checks", [])),
            )
            for code in sorted(semantic_review_marks(review)):
                validation.mark(code)
        unsupported = [check for check in review.get("checks", []) if not check["supported"]]
        review["issues"] = [
            *review["issues"],
            *[check["text"] + "：" + check["basis"] for check in unsupported],
        ]
        if review["accepted"] and not review["issues"]:
            delivery = review.get("delivery")
            if delivery:
                if delivery.get("addressee_id") not in {None, *known_ids}:
                    raise ProviderError("回应对象不在当前可见场景，未提交片段。")
                intent.update(
                    {k: v for k, v in delivery.items() if k != "remaining_request" or v is not None}
                )
                intent["wait_for_player"] = delivery.get("addressee_id") in {None, turn.player_id}
            break
        if semantic_repairs >= 2:
            raise UnsafeDraftExhausted("NPC回答仍有无依据的剧情细节，此片段未提交；此前已显示的发言保留。")
        semantic_repairs += 1
        repair_instruction = (
            "这是同一片段的最后一次修复。请只对玩家已作出的选择作简短、自然的回应；"
            "若尚未观察到天气、物品或他人行为，不描述其当前状态。"
            "没有把握的句子直接删掉，不以另一句现场描述替换。\n"
            if semantic_repairs == 2
            else "后台质量检查（数据）：只删除或修正指出的无据断言，保留其余正确回答和自然表达；"
            "不要为了修复添加新的历史、物证、现场细节、公告内容或核查经过。"
            "被指出的细节若无来源，直接删去相应句子；无需换一个同样无据的细节填空。\n"
        )
        draft_context = [
            *context,
            {
                "role": "user",
                "content": repair_instruction
                + json.dumps(
                    {
                        "rejected_candidate": {"text": speech["text"], "claims": speech["claims"]},
                        "issues": list(dict.fromkeys(review["issues"])),
                    },
                    ensure_ascii=False,
                ),
            },
        ]
    answer = re.sub(r"^\s*" + re.escape(info["profile"]["name"]) + r"[：:]\s*", "", answer)
    runtime.check(task.id, task.lease_version)
    return {
        "id": actor.id,
        "name": info["profile"]["name"],
        "revision": info["actor_revision"],
        "answer": normalize_citations(answer, evidence),
        "spoken_parts": speech.get("spoken_parts", []),
        "performance": speech.get("performance", [])
        if [part["text"] for part in speech.get("performance", [])] == speech.get("spoken_parts", [])
        else [],
        "claims": [
            {**claim, "source_id": evidence_handles.get(claim["source_id"], claim["source_id"])}
            for claim in speech["claims"]
        ],
        "evidence": evidence,
        "missing": missing,
        **({"_candidate_audio": private_audio, "_audio_status": audio_status} if voice_service else {}),
    }
