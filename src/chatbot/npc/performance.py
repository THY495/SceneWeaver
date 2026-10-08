"""Prepare one actor response or action; all world commits stay in the coordinator."""

import asyncio
import json
import re

from ..context import ContextPressure
from ..evidence import model_evidence, normalize_citations
from ..observability.timing import annotate, measure, timed
from ..providers import OutputTruncated, ProviderError, estimate_tokens
from ..role_policy import (
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
from .intent_recovery import (
    SpeechPurposeRepair,
    execution_intent,
    purpose_recovery_messages,
    unsupported_candidate_checks,
)
from .item_expression import (
    RESULT_EXPRESSION,
    RESULT_LANGUAGE,
    bind_result_quotes,
    expression_token_budget,
    result_candidates,
    result_focus,
)
from .review import review_messages
from .speech_contract import performance_policy
from .speech_projection import project_speech


class UnsafeDraftExhausted(ProviderError):
    """A rejected performance cannot be published under its current task."""


class ResponseContractConflict(UnsafeDraftExhausted):
    """A reviewed player question needs a different authorized turn contract."""


def requires_contract_replanning(review, intent, assignment, player_id):
    """Only a clean review of an intended player question can change tasks."""
    delivery = review.get("delivery") or {}
    return bool(
        player_id
        and (assignment or {}).get("response_utterance")
        and assignment.get("response_turn_policy") == "answer_only"
        and assignment.get("addressees") == [player_id]
        and intent.get("addressee_id") == player_id
        and intent.get("expects_reply") is True
        and delivery.get("addressee_id") == player_id
        and delivery.get("expects_reply") is True
        and review.get("provider_semantic_clean") is True
        and review.get("scheduling_issues") == ["response_contract_reply_mismatch"]
    )


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
    initial_speech: dict | None = None,
    voice_service=None,
    voice: str = "",
    director_assignment: dict | None = None,
):
    annotate(actor_id=actor.id, step=step, voice_candidate_enabled=voice_service is not None)
    # Grounded response tasks have an actual utterance to address. Whole-part
    # deletion cannot prove the surviving mood/comment still fulfils that task.
    # Keep bounded repair plus full review rather than promoting this remainder.
    grounded_response = bool((director_assignment or {}).get("response_utterance")
        and (director_assignment or {}).get("response_turn_policy") in {"answer_only", "invite_player"})
    annotate(grounded_response_pruning_disabled=grounded_response)
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
    working_basis = None
    if initial_speech is not None:
        from .combined_generation import basis_digest

        working_basis = basis_digest(info, context)
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
    context[0]["content"] += performance_policy(task)
    intent_context_index = len(context)
    context.append(
        {
            "role": "user",
            "content": "本次意图建议（数据，不是事实）：" + json.dumps(intent, ensure_ascii=False),
        }
    )
    if director_assignment is not None:
        context.append(
            {
                "role": "user",
                "content": "已通过本角色知情检查的单段演出任务（数据，不是事实或强制立场）："
                + json.dumps(director_assignment, ensure_ascii=False)
                + "\n结合自身意图，仅完成本片段的一次回应，面向一个主要接收者。"
                "任务的addressees只有一人时，需要答复的问题也只向此人；"
                "可提及其他人的话，不另向其他人请求答复或行动。"
                "遵守delivery_constraints中的表达长度与节奏；不要把这些后台要求念给角色听。"
                "演出交接由后台处理；台词只表达人物此刻的态度和意图，并以场景内的自然语言收尾。"
                "回答任务中的具体问题，不知答案时明确说明未知或需核实，不能只复述对方的意愿。"
                "完成确认或收尾就自然结束，不习惯性附加新问题；只有本片段目的确实需要回答时才提问。"
                "若dialogue_capacity.can_schedule_more=false，对不在scheduled_reply_actor_ids中的NPC，"
                "不要再提出本段待答问题；可表达希望稍后商量、承认未知或保留意见，不能替对方回答。"
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
    from .speech_packets import PacketSpeech, output_contract, to_actor_speech, uses_packets

    packets = uses_packets(services)
    context[0]["content"] += output_contract(packets)
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
    # A combined candidate is private and goes through the exact same local,
    # semantic and audio gates. A changed working-set revision invalidates it.
    if initial_speech is not None:
        if (
            initial_speech.get("actor_revision") != info["actor_revision"]
            or initial_speech.get("basis_digest") != working_basis
            or intent.get("retrieval") != "context"
            or intent.get("act") != "speak"
        ):
            initial_speech = None
            annotate(combined_speech_reused=False, generation_fallback="stale_or_ineligible")
    citation_repairs = semantic_repairs = 0
    pruned_candidate = None
    pruning_used = False
    purpose_recovery_used = False
    private_audio, audio_status = None, "not_requested"

    async def generate_speech(messages, **kwargs):
        if packets and kwargs.get("operation"):
            kwargs["operation"] += ":speech-packets-v1"
        value = await services.provider.chat(
            task.id, "dialogue", messages, schema=PacketSpeech if packets else ActorSpeech, **kwargs
        )
        return to_actor_speech(value) if packets else value

    # One local citation repair, one exact part pruning and up to two semantic repairs may be needed
    # for the same candidate. Every revision still passes both reviews before
    # publication; this is a bounded recovery path, not a review bypass.
    for attempt in range(5):
        output_tokens = expression_token_budget(focused_result, info)
        try:
            if pruned_candidate is not None:
                speech, pruned_candidate = pruned_candidate, None
            elif initial_speech is not None:
                speech = ActorSpeech.model_validate(initial_speech["speech"]).model_dump()
                initial_speech = None
                annotate(combined_speech_reused=True)
            else:
                speech = await generate_speech(
                    draft_context,
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
                speech = await generate_speech(
                    draft_context,
                    max_tokens=min(2400, output_tokens + 1200),
                    operation=f"{operation_scope}npc:{actor.id}:{step}:answer:{attempt}:expanded",
                )
        speech = bind_result_quotes(speech, focused_result)
        from .speech_references import normalize_own_profile_reference

        speech, repaired_references = normalize_own_profile_reference(speech, actor.id, sources)
        annotate(profile_reference_repair_count=repaired_references)
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
            if not pruning_used and not grounded_response:
                from .speech_pruning import prune_local_fact_parts

                with measure("npc.local_fact_pruning") as pruning:
                    pruned_candidate = prune_local_fact_parts(
                        speech, invalid, sources, info.get("observations", []), task.payload.get("text", "")
                    )
                    pruning.mark("candidate_retained" if pruned_candidate is not None else "declined")
                if pruned_candidate is not None:
                    pruning_used = True
                    continue
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
                    "若含notice_receipt_unclaimed或notice_receipt_not_in_quote，"
                    "当前‘通知没到/我没收到’须有明确否定接收依据；"
                    "‘收到通知后公布’只规定条件，不证明现在尚未收到。"
                    "无此依据则改成自身当前不掌握时间或无法核实通知，不补造过去接收经历。"
                    "若含weather_state或reading_history相关错误，风雨等当前状态和是否读过/看清"
                    "具体材料都须有依据，不能把它们称为自由感受来放行。"
                    "无据时仍回应原请求，明确本人当前不知道风势或文字内容；不要补造替代现象或阅读过程。"
                    "claims.text必须逐字出现在最终台词text中；"
                    "quote必须逐字出现在对应来源。台词中用引号引用历史原话也必须逐字对应原始来源；"
                    "若只是概括就不要伪装成直接引文。speech来源只能证明曾经说过，"
                    "不能用mode=fact/action证明话中内容；转述使用reported并在台词明确归属。"
                    "玩家听闻不能变成你自己的听闻；你自己的旧说法也不能证明现在确实如此。"
                    "没有独立证据的事实应承认不知道，不重复断言。"
                    "未知原文用角色在场的自然语言说清当前未知，不补造过去是否读过的经历。"
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

        async def review_call():
            import hashlib

            from .response_review import BoundActorReview, enforce_binding, response_source

            response_binding = bool(getattr(services.config, "role_response_review_binding", False)
                and response_source(director_assignment) is not None)
            messages = review_messages(
                info, model_evidence(evidence), speech, sources, actor_id=actor.id, documents=evidence,
                intent=execution_intent(intent, recovered=purpose_recovery_used),
                player_request=task.payload.get("text", "") if turn.player_id in known_ids else "",
                cast=known_cast, director_assignment=director_assignment, response_binding=response_binding,
            )
            # A verdict belongs to this exact candidate, grounding and review
            # contract. Restarting after a policy/context change cannot rebind it.
            review_basis = hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
            review = await services.provider.chat(
                task.id,
                "actor_review",
                messages,
                schema=BoundActorReview if response_binding else ActorReview,
                max_tokens=1000,
                operation=f"{operation_scope}npc:{actor.id}:{step}:review:{attempt}:{review_basis}",
            )
            delivery = review.get("delivery") or {}
            target = delivery.get("addressee_id")
            # Cached provider output precedes deterministic scheduling checks.
            # Keep these rejections distinct from model factual judgments.
            provider_accepted = review["accepted"]
            provider_semantic_clean = bool(
                review["accepted"] and not review.get("issues", [])
                and all(check["supported"] for check in review.get("checks", []))
            )
            scheduling_issues = []
            if response_binding:
                review, response_issues = enforce_binding(review, director_assignment, speech["text"])
                raw_assessment = review.get("response_assessment")
                assessment = raw_assessment if isinstance(raw_assessment, dict) else {}
                status = assessment.get("status")
                annotate(response_review_binding=True, response_binding_accepted=not response_issues,
                         response_task_status=status if status in {
                             "responded", "refused", "deferred", "unknown", "not_addressed"} else None)
                if response_issues:
                    scheduling_issues.extend(response_issues)
            from .dialogue_capacity import unavailable_npc_request

            if (
                review["accepted"]
                and (director_assignment or {}).get("response_turn_policy") == "answer_only"
                and (delivery if delivery else intent).get("expects_reply")
            ):
                scheduling_issues.append("response_contract_reply_mismatch")
                review = {
                    **review,
                    "accepted": False,
                    "issues": [
                        *review.get("issues", []),
                        "本次回应契约要求回答、拒绝、暂缓或承认未知后收束；"
                        "当前台词额外请求接收者答复，请删去该请求并保留实际回应。",
                    ],
                }
            if review["accepted"] and unavailable_npc_request(
                delivery, (director_assignment or {}).get("dialogue_capacity"), turn.player_id
            ):
                scheduling_issues.append("reply_capacity_exhausted")
                review = {
                    **review,
                    "accepted": False,
                    "issues": [
                        *review.get("issues", []),
                        "本段没有为这位NPC安排后续答复位置。保留已完成的回答、未知或保留意见，"
                        "将非必需商量留待后续，不新增本段待答问题，也不替对方回答。",
                    ],
                }
            assigned_to = (director_assignment or {}).get("addressees") or []
            if (
                review["accepted"]
                and delivery
                and len(assigned_to) == 1
                and delivery.get("response_kind") != "silence"
                and target != assigned_to[0]
            ):
                scheduling_issues.append("director_addressee_mismatch")
                review = {
                    **review,
                    "accepted": False,
                    "issues": [
                        *review.get("issues", []),
                        "本片段应直接面向导演指定的那位角色表达；可提到其他在场人，"
                        "但不要把回应变成向玩家或旁观者报告。",
                    ],
                }
            if review["accepted"] and delivery.get("expects_reply") and target and target != turn.player_id:
                from ..event_projection import requests_for

                with db.session() as s:
                    already_waiting = any(
                        request["task_id"] == task.id
                        and request["target_id"] == target
                        and request["status"] in {"open", "partial", "defer"}
                        for request in requests_for(s, target)
                    )
                if already_waiting:
                    scheduling_issues.append("duplicate_npc_request")
                    review = {
                        **review,
                        "accepted": False,
                        "issues": [
                            *review.get("issues", []),
                            "该接收者本轮已有待答问题；本片段请自然回应，不再向其提出新的待答请求。",
                        ],
                    }
            return {**review, "provider_accepted": provider_accepted,
                    "provider_semantic_clean": provider_semantic_clean, "scheduling_issues": scheduling_issues}

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
            with db.session() as s:
                live_task = s.get(Task, task.id)
                calls_used = live_task.calls if live_task else services.config.max_calls_per_task
            review_first = attempt > 0 or calls_used >= max(0, services.config.max_calls_per_task - 12)
            annotate(voice_review_first=review_first)
            review, private_audio, audio_status = await reviewed_audio(
                identity,
                review_call,
                (lambda: voice_service.stream_candidate(task, first, voice, direction)) if first else None,
                is_current,
                discard_audio=voice_service.discard_candidate,
                synthesize_after_review=review_first,
            )
            if projection_failed:
                audio_status = "failed"
                annotate(audio_status=audio_status, projection_status="failed")
        with measure("npc.semantic_review_result") as validation:
            annotate(
                candidate_revision=attempt,
                review_accepted="semantic_accepted" in semantic_review_marks(review),
                review_provider_accepted=review["provider_accepted"],
                review_issue_count=len(review.get("issues", [])),
                review_unsupported_count=sum(not check["supported"] for check in review.get("checks", [])),
            )
            assessment = review.get("response_assessment")
            if getattr(services.config, "role_response_review_binding", False) and isinstance(assessment, dict):
                status = assessment.get("status")
                annotate(response_review_binding=True, response_binding_accepted=not any(
                    code.startswith("response_binding_") or code == "response_task_not_addressed"
                    for code in review["scheduling_issues"]), response_task_status=status if status in {
                        "responded", "refused", "deferred", "unknown", "not_addressed"} else None)
            for code in sorted(semantic_review_marks(review)):
                validation.mark(code)
            for code in review["scheduling_issues"]:
                validation.mark(code)
        unsupported = [check for check in review.get("checks", []) if not check["supported"]]
        review["issues"] = [
            *review["issues"],
            *[check["text"] + "：" + check["basis"] for check in unsupported],
        ]
        if requires_contract_replanning(review, intent, director_assignment, turn.player_id):
            # Full review has already rejected and discarded speculative audio.
            # Reconsider the intended turn instead of exhausting rewrites under
            # the same task. The director still decides whether to ask or answer.
            with measure("npc.response_contract_recovery") as recovery:
                recovery.mark("intended_player_question")
            raise ResponseContractConflict("本片段的提问方式与已授权回应契约冲突，未提交台词。")
        if review["accepted"] and not review["issues"]:
            delivery = review.get("delivery")
            if delivery:
                if delivery.get("addressee_id") not in {None, *known_ids}:
                    raise ProviderError("回应对象不在当前可见场景，未提交片段。")
                intent.update(
                    {k: v for k, v in delivery.items() if k != "remaining_request" or v is not None}
                )
                intent["wait_for_player"] = delivery.get("addressee_id") in {None, turn.player_id}
                if delivery.get("requires_player_action") is not None:
                    intent["awaiting_player_action"] = bool(
                        delivery["requires_player_action"]
                        and delivery.get("expects_reply")
                        and delivery.get("addressee_id") == turn.player_id
                    )
            break
        if not pruning_used and not grounded_response:
            from .speech_pruning import prune_rejected_parts

            with measure("npc.review_pruning") as pruning:
                pruned_candidate = prune_rejected_parts(speech, review)
                pruning.mark("candidate_retained" if pruned_candidate is not None else "declined")
            if pruned_candidate is not None:
                pruning_used = True
                continue
        if semantic_repairs >= 2:
            raise UnsafeDraftExhausted("NPC回答仍有无依据的剧情细节，此片段未提交；此前已显示的发言保留。")
        semantic_repairs += 1
        rejected_checks = unsupported_candidate_checks(review, speech)
        if director_assignment is None and rejected_checks and not purpose_recovery_used:
            purpose_recovery_used = True
            with measure("npc.purpose_recovery") as recovery:
                recovery.mark("unsupported_candidate_fact")
                repaired = SpeechPurposeRepair.model_validate(
                    await services.provider.chat(
                        task.id,
                        "role_intent",
                        purpose_recovery_messages(
                            info,
                            intent,
                            task.payload.get("text", "") if turn.player_id in known_ids else "",
                            rejected_checks,
                            model_evidence(evidence),
                        ),
                        schema=SpeechPurposeRepair,
                        max_tokens=350,
                        operation=f"{operation_scope}npc:{actor.id}:{step}:purpose-recovery-v1",
                    )
                )
                intent["purpose"] = repaired.purpose
                context[intent_context_index] = {
                    "role": "user",
                    "content": "本次意图建议（数据，不是事实）："
                    + json.dumps(execution_intent(intent, recovered=True), ensure_ascii=False),
                }
                recovery.mark("purpose_replaced")
            draft_context = [
                *context,
                {
                    "role": "user",
                    "content": "重新生成本片段台词。回应原玩家请求，面向原接收者；"
                    "只依据已授权资料，未知就用人物自己的话明确当前未知。"
                    "上一稿无据事实已废弃，不保留其现场描写；不新增待答问题。",
                },
            ]
            continue
        repair_instruction = (
            "这是同一片段的最后一次修复。保留本片段的指定接收者和目的，简短、自然地回答、"
            "拒绝或说明当前未知；不要把面向NPC的回应改成向玩家报告，不新增待答问题。"
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
    else:
        raise UnsafeDraftExhausted("候选修复次数已耗尽，未审查通过的台词不提交。")
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
