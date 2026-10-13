"""Shared roleplay semantics; permissions and citations remain independently enforced."""

import re

from .provenance import cannot_establish_objective_claim, source_class

ROLE_EXPRESSION_POLICY = """
关键设定严格、局部表达自由。允许符合角色和当前情境的表情、语气、停顿及不改变世界状态的小动作；
不要求这些表现逐字有据。不得借局部表现新增过去经历、关键物证、人物关系、物品状态或玩家行动。
区分事实断言、转述、主观信念、假设、提问、提议、单方承诺和已完成行动。
没有阅读记录不证明角色“从未看过”材料；这种过去未发生的断言仍需角色经历依据。
角色可以表达当下无法核实或不掌握原文，不必为承认不确定性提供一次过去未阅读的记录；已有相反确认事实除外。
人物身份不自动赋予未设定的场所规则、必要物品、能力或权限。
“必须先找到某物才能进入”“缺少某条件就无法继续”若被说成世界规则，需要已知依据；
改说“我现在还差某物”仍在断言前提，不能标成proposal来免除依据检查。
“还差X”“给我X就接着做”把X设成后续行动的条件；语气温和、由本人提出或允许稍后提供，
都不能使未设定的条件自动成立。旧发言只证明角色曾提出过，不证明它是有效规则。
角色可以提出明确可拒绝的个人建议，但不能把建议变成已设定的必经步骤，
也不能保证未经确认的结果。玩家可放弃整个行动，不表示角色列出的前置条件是可选的。
“不提供X也不影响Y”是在断言X与Y的规则关系；尊重对方选择只证明不会强迫，
不证明世界结果必然不受影响，仍需依据。
“供将来做Y”仅说明建议的可能用途，不保证Y会发生；未预设的、可拒绝且不改变世界规则的小建议可提出。
“稍后再拿来”只调整时间，不使无据前提成立；此前角色自己说过也不证明它为真。
作者已明确提供的规则可按原范围说明，未知规则不能借常识补造。
提问和假设不代表事件发生；但“你为什么偷了钥匙”预设偷窃，需要检查这个预设。
角色可基于已有个人信念误解事实，必须有该信念依据；不得以“我觉得”包装任意新增线索。
有动机的谎言只构成发言事件，不修改世界真相。来源原文存在不等于支持断言的时间、主体和完成状态。
想做不等于答应，单方答应不等于双方约定，约定不等于已执行。人物失踪十年不证明自己寻找了十年。
发现于17:35、移交于17:38是两个不同事件，不得合并时间。重点核对这些语义，而非要求措辞相同。
工作集、观察记录和来源ID是系统的呈现方式，不是世界中的文件。听到发言应说听到，
不能因为工作集写着记录就声称看过记录、查过日志；只有明确的阅读经历或实际检索材料支持时才可这样说。
知道某个事实与如何获知是两个断言，不能擅自补造获知过程；匿名听闻不能被补成已辨认的说话者。
"""


# The actor needs a compact, in-world performance brief. Detailed adversarial
# examples above belong to the reviewer and assignment checks, not the voice
# that the player hears.
ROLE_PERFORMANCE_POLICY = """
关键设定严格、局部表达自由。依据角色经历、实际见闻和已确认的世界状态说话。
不知道外部状态时只说不知道；不要把“我不知道”说成“风还没停”或“通知还没来”。
表情、停顿和不改变世界状态的小动作可按人物性格自然发挥。
历史言论只证明有人这样说过；传闻与个人判断保留它们原有的不确定性。
人物身份不自动提供未写明的世界规则、经历或物证。当前的提议与承诺可以产生，
但不能说成早已完成的行动或双方已有的约定。玩家的选择和行动由玩家自己决定。
未知之处用人物在场会说的话表达，并可提出眼下能做的事；台词始终留在故事世界里。
"""


# Runtime director handoff is a control action, never dialogue. Limit this
# guard to the recognizable control phrases; ordinary in-world turns such as
# "轮到你说了" must remain available to a character.
_DIRECTOR_HANDOFF_LEAK = re.compile(
    r"(?:场景|演出|回合|控制权).{0,8}(?:交还|交回|还给|交给).{0,8}(?:玩家|旅人|你)"
    r"|(?:交还|交回)(?:玩家|旅人)(?:的)?(?:场景|回合|控制权)"
)


def director_handoff_leak(text: str) -> bool:
    return bool(_DIRECTOR_HANDOFF_LEAK.search(text))


# Generated parenthetical stage directions are visual performance, not an item
# executor. Restrict only explicit object transitions; ordinary body language
# remains free. A named item still has to pass through the world action path.
_STAGE_DIRECTION = re.compile(r"[（(]([^（）()]*)[）)]")
_STAGE_OBJECT_MUTATION = re.compile(
    r"(?:把|将)(.{1,16}?)(?:按灭|点燃|拿起|拾起|捡起|放下|递给|交给|藏起|藏进|"
    r"扔掉|销毁|撕毁|撕碎|捏皱|揉皱|打开|关上|合上|取出|放进|装进|塞进)"
)
_STAGE_HELD_OBJECT_MUTATION = re.compile(
    r"(?:按灭|点燃|放下|拿起|拾起|捡起|递出|藏起|扔掉|撕毁|撕碎|捏皱|揉皱|打开|关上|合上)"
    r"(?:了)?(?:自己)?手(?:里|中)(?:的)?([^，。；、]{1,12})"
    r"|手(?:里|中)(?:的)?([^，。；、]{1,12}?)(?:被)?(?:捏皱|撕碎|揉皱|递出|扔掉)"
)
_BODY_EXPRESSION = re.compile(
    r"^(?:自己(?:的)?|他(?:的)?|她(?:的)?|一只|双)?"
    r"(?:手|头|眼|眼睛|目光|视线|脸|肩|身体|呼吸|声音|语速|话)$"
)


def unexecuted_stage_object_action(text: str) -> bool:
    """Reject an explicit prop mutation disguised as free stage performance."""
    for direction in _STAGE_DIRECTION.findall(text):
        for match in _STAGE_OBJECT_MUTATION.finditer(direction):
            if not _BODY_EXPRESSION.fullmatch(match.group(1).strip()):
                return True
        for match in _STAGE_HELD_OBJECT_MUTATION.finditer(direction):
            object_name = (match.group(1) or match.group(2)).strip()
            if object_name not in {"事", "工作", "活儿", "话"} and not _BODY_EXPRESSION.fullmatch(
                object_name
            ):
                return True
    return False


REVIEW_REASONS = frozenset(
    {
        "unsupported_fact",
        "invented_history",
        "acquisition_mismatch",
        "statement_promotion",
        "agreement_promotion",
        "player_agency",
        "mixed_recipient",
        "other",
    }
)


def semantic_review_marks(review):
    """Fixed labels only. These describe the reviewer, not objective correctness."""
    unsupported = [c for c in review.get("checks", []) if not c["supported"]]
    rejected = not review["accepted"] or bool(review.get("issues")) or bool(unsupported)
    marks = {"semantic_rejected" if rejected else "semantic_accepted"}
    if not review.get("provider_accepted", review["accepted"]):
        marks.add("review_flag_rejected")
    if review.get("issues"):
        marks.add("review_has_issues")
    if unsupported:
        marks.add("unsupported_check")
        for check in unsupported:
            reason = check.get("reason_code")
            marks.add("reason_" + (reason if reason in REVIEW_REASONS else "unclassified"))
    return marks


def needs_knowledge(intent):
    """Conservative default for old cached intents; an inspection always performs retrieval."""
    return intent.get("act") == "inspect" or intent.get("retrieval", "knowledge") != "context"


def invalid_claims(speech, sources):
    return [
        claim
        for claim in speech["claims"]
        if claim["source_id"] not in sources
        or not claim["quote"].strip()
        or claim["quote"] not in sources[claim["source_id"]]
        or claim["text"] not in speech["text"]
    ]


_NEGATIVE_NOTICE_RECEIPT = re.compile(
    r"(?:通知|消息)(?:目前|现在|至今|仍然|依然|还)?(?:还没|尚未|未|没有|没)(?:到|来|送达|收到|下来|下达)"
    r"|(?<!有)(?:还没|尚未|未|没有|没)(?:收到|接到).{0,6}(?:通知|消息)"
    r"|复航(?:的)?时间[，, ]?(?:我|我们|本人)?(?:目前|现在|眼下|也|还|仍然|仍)?"
    r"(?:还没|尚未|未|没有|没)(?:收到|接到)"
)


def asserted_notice_nonreceipt(text):
    """A receipt assertion needs evidence; a local explicit hypothesis does not.

    This deliberately recognizes a narrow notice/time vocabulary, not general
    negative history or arbitrary pronoun resolution.
    """
    for match in _NEGATIVE_NOTICE_RECEIPT.finditer(text):
        prefix = re.split(r"[。！？!?；;，,\n]", text[: match.start()])[-1]
        if not re.search(r"(?:如果|假如|若是|要是|倘若|若)[^，,。！？!?；;\n]{0,12}$", prefix):
            return True
    return False


def claim_issues(speech, sources, observations, question):
    """All deterministic rejection causes; indexes refer only to this candidate.

    No candidate text, source IDs or quotations enter persisted timing records.
    These diagnostics explain existing checks; they do not authorize a claim.
    """
    from .npc.negative_fact_guard import negative_fact_issues

    issues = negative_fact_issues(speech)
    promoted = invalid_statement_promotions(speech, observations)
    if asserted_notice_nonreceipt(speech["text"]) and not any(
        asserted_notice_nonreceipt(claim["text"]) for claim in speech["claims"]
    ):
        # The reviewer sometimes treats a conditional announcement as proof
        # of present nonreceipt when the draft omits this assertion from claims.
        # Require a cited claim so the quote-level check below can run.
        issues.append({"code": "notice_receipt_unclaimed"})
    for index, claim in enumerate(speech["claims"]):
        codes = []
        if claim["source_id"] not in sources:
            codes.append("source_unavailable")
        elif not claim["quote"].strip():
            codes.append("quote_empty")
        elif claim["quote"] not in sources[claim["source_id"]]:
            codes.append("quote_mismatch")
        if claim["text"] not in speech["text"]:
            codes.append("claim_text_mismatch")
        if claim in promoted:
            codes.append("statement_promoted")
        if asserted_notice_nonreceipt(claim["text"]) and not asserted_notice_nonreceipt(claim["quote"]):
            # A conditional source ("after receiving a notice") does not prove
            # that the notice has not arrived. Keep this narrow: uncertainty
            # about receipt and a source that explicitly confirms nonreceipt
            # remain available to the character.
            codes.append("notice_receipt_not_in_quote")
        issues.extend({"claim_index": index, "code": code} for code in codes)
    if invalid_recalled_quotes(speech["text"], sources, question):
        issues.append({"code": "recalled_quote_mismatch"})
    return issues


def citation_repair_sources(speech, sources, issues, question=""):
    """Repeat bounded authorized excerpts for broken claim or history quotes.

    History quotes need an exact, present claim connecting the rejected span
    to its source. This supplies repair evidence, never quotation approval.
    """
    result = {}
    history_quotes = (
        invalid_recalled_quotes(speech["text"], sources, question)
        if any(issue["code"] == "recalled_quote_mismatch" for issue in issues)
        else []
    )
    for issue in issues:
        if issue["code"] in {"quote_empty", "quote_mismatch"}:
            candidates = [speech["claims"][issue["claim_index"]]]
        elif issue["code"] == "recalled_quote_mismatch":
            candidates = [
                claim
                for claim in speech["claims"]
                if claim["text"] in speech["text"]
                and any(span in claim["text"] for span in history_quotes)
                and claim["quote"].strip()
                and claim["quote"] in sources.get(claim["source_id"], "")
            ]
        else:
            continue
        for claim in candidates:
            source_id = claim["source_id"]
            if source_id in sources and source_id not in result and len(result) < 4:
                text = sources[source_id]
                result[source_id] = {"text": text[:4000], "excerpt_truncated": len(text) > 4000}
    return result


def invalid_statement_promotions(speech, observations):
    """An utterance can ground reported speech, never independently prove its contents."""
    statement_ids = {e["id"] for e in observations if cannot_establish_objective_claim(e)}
    return [
        c
        for c in speech["claims"]
        if c["source_id"] in statement_ids and c.get("mode", "fact") in {"fact", "action"}
    ]


def review_grounding(info, evidence, speech, sources):
    observations = {e["id"]: e for e in info.get("observations", [])}
    documents = {e["text"]: e["source"] for e in evidence if "text" in e and "source" in e}
    facts = {
        k: (
            {field: value[field] for field in ("event_id", "value", "basis", "occurred_at") if field in value}
            if isinstance(value, dict)
            else {"value": value}
        )
        for k, value in info.get("scene_facts", {}).items()
    }
    fact_texts = {v["value"]: k for k, v in facts.items() if "value" in v}
    referenced = []
    seen = set()
    for claim in speech["claims"]:
        source_id = claim["source_id"]
        if source_id in seen:
            continue
        seen.add(source_id)
        observation = observations.get(source_id)
        source_text = sources.get(source_id, "")
        reference = None
        if source_text in documents:
            reference = {"document": documents[source_text]}
        elif source_text in fact_texts:
            reference = {"confirmed_event": fact_texts[source_text]}
        referenced.append(
            {
                "id": source_id,
                "kind": observation["kind"] if observation else "profile_host_or_document",
                "source_class": source_class(observation)
                if observation
                else "resolve_from_primary_grounding",
                **({"already_in_primary": reference} if reference else {"text": source_text}),
                **utterance_observation(observation),
                **(
                    {"addressees": observation.get("addressees", [])}
                    if observation and observation.get("kind") == "speech"
                    else {}
                ),
                "detail": {
                    k: v
                    for k, v in (observation.get("detail", {}) if observation else {}).items()
                    if k in {"basis", "key", "value", "from", "to", "object"}
                },
            }
        )
    return {
        "primary_grounding": {
            "profile": info["profile"],
            "confirmed_events": facts,
            "known_objects": info.get("known_objects", []),
            "item_action_results": [
                {"id": e["id"], "text": e["text"], "result": e["detail"]["item_action_result"]}
                for e in observations.values()
                if source_class(e) == "item_action_result"
            ],
            "documents": evidence,
        },
        "referenced_records_with_provenance": referenced,
        "recent_observed_utterances": recent_review_utterances(info, sources),
        "subjective_goals_not_completed_actions": info.get("active_goals", {}),
        "pending_replies": info.get("pending_replies", []),
        "player_statement_history": (
            {
                "items": [
                    {k: e[k] for k in ("id", "seq", "actor_id", "speaker", "text", "occurred_at") if k in e}
                    for e in info["player_statement_history"]["items"]
                ],
                "more_matching_events": info["player_statement_history"]["more_matching_events"],
            }
            if info.get("player_statement_history")
            else None
        ),
    }


def recent_review_utterances(info, sources):
    """Bounded authorized history, including utterances absent from candidate claims."""
    recent = []
    remaining = 4000
    observations = [
        e
        for e in info.get("observations", [])
        if e.get("kind") == "speech" and e.get("id") in sources and "actor_id" in e
    ]
    for event in reversed(observations[-8:]):
        if not remaining:
            break
        text = event["text"][:remaining]
        truncated = len(text) < len(event["text"])
        attribution = utterance_observation(event)
        if truncated:
            attribution["utterance_observation"]["complete"] = False
        recent.append(
            {
                "id": event["id"],
                "text": text,
                "excerpt_truncated": truncated,
                "addressees": event.get("addressees", []),
                **attribution,
            }
        )
        remaining -= len(text)
    return list(reversed(recent))


def utterance_observation(observation):
    """Preserve the observer-scoped view; never look up the canonical speaker.

    Old/synthetic inputs without attribution metadata cannot grant a witnessed
    acquisition path. Perception excerpts stay bounded by the existing source.
    """
    if not observation or observation.get("kind") != "speech" or "actor_id" not in observation:
        return {}
    perception = observation.get("perception", {})
    known = bool(observation["actor_id"]) and perception.get("speaker_known", True)
    return {
        "utterance_observation": {
            "acquisition": "observed_utterance",
            "speaker_known": known,
            "actor_id": observation["actor_id"] if known else None,
            "speaker": observation.get("speaker") if known else None,
            "complete": perception.get("complete", True),
            "contents_verified": False,
        }
    }


def invalid_recalled_quotes(answer, sources, question):
    """A history lookup may paraphrase, but quotation marks promise a literal source span."""
    if not any(term in question for term in ("原话", "说法", "说过", "改口", "转述")):
        return []
    spans = re.findall(r'“([^”]+)”|「([^」]+)」|『([^』]+)』|"([^"\n]+)"', answer)
    quoted = [next(part for part in match if part) for match in spans]
    return [span for span in quoted if not any(span in text for text in sources.values())]
