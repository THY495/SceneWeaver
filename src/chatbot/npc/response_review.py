"""Candidate/source binding for the existing reviewer; not semantic proof."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, ValidationError

from ..schemas import ActorReview, SpeechDelivery

RESPONSE_BINDING_SYSTEM = (
    "本次另填写response_assessment：核对authorized_performance_requirement.response_utterance这条实际发言，"
    "source_event_id逐字使用它的event_id；它可能是提问、问候或陈述。"
    "status=responded表示候选已经回应本条发言，不要求每次提供事实答案；refused表示明确拒绝当前请求，"
    "deferred表示明确暂缓当前请求，unknown表示明确说明本人对当前所问未知；not_addressed表示没有回应。"
    "quotes摘最终candidate中实际完成对应回应的连续原文；basis简述它如何对应当前发言。"
    "普通情绪或天气感想不是任意问题的答案；例如问职业而只说‘今天看着要变天’，应not_addressed，"
    "accepted=false，issues说明实际回应缺失，即使事实检查全部通过、quotes逐字存在。"
    "其余状态须有非空quotes。拒绝、暂缓、未知只有确实指向当前请求才成立，不能用一句无关‘不知道’冒充。"
    "not_addressed也可摘无关原文说明问题，不创建candidate中不存在的事实checks。"
    "保留全部事实、知情、角色自主权和表达目标核查；回应主问题不允许附加编造。"
    "delivery.response_kind须明确填写：responded/unknown对应answer，refused对应refuse，deferred对应defer；"
    "新状态只评价此候选，不表示世界动作成功、作者段落完成或旧请求已经关闭。"
)


class ResponseAssessment(BaseModel):
    source_event_id: str = Field(min_length=1, max_length=100)
    status: Literal["responded", "refused", "deferred", "unknown", "not_addressed"]
    quotes: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(max_length=3)
    basis: str = Field(min_length=1, max_length=350)


class BoundSpeechDelivery(SpeechDelivery):
    # No legacy default: the bound contract checks an explicit reviewer choice.
    response_kind: Literal["answer", "refuse", "defer", "silence"] = Field(...)


class BoundActorReview(ActorReview):
    delivery: BoundSpeechDelivery
    response_assessment: ResponseAssessment


def response_source(assignment):
    if not assignment or assignment.get("response_turn_policy") != "answer_only":
        return None
    source = assignment.get("response_utterance")
    if (not isinstance(source, dict) or not isinstance(source.get("event_id"), str)
            or not source["event_id"] or not isinstance(source.get("text"), str)):
        return None
    return source


def binding_issues(review, assignment, candidate):
    """Validate identity/quotes/explicit verdict; never infer relevance from words."""
    source = response_source(assignment)
    if source is None:
        return ["response_binding_source_invalid"]
    try:
        bound = BoundActorReview.model_validate(review)
    except ValidationError:
        return ["response_binding_missing_or_invalid"]
    assessment = bound.response_assessment
    issues = []
    if assessment.source_event_id != source["event_id"]:
        issues.append("response_binding_source_mismatch")
    if any(quote not in candidate for quote in assessment.quotes):
        issues.append("response_binding_quote_mismatch")
    if any(not quote.strip() for quote in assessment.quotes):
        issues.append("response_binding_quote_blank")
    if assessment.status == "not_addressed":
        issues.append("response_task_not_addressed")
    elif not assessment.quotes:
        issues.append("response_binding_quote_missing")
    expected = {"responded": "answer", "unknown": "answer", "refused": "refuse", "deferred": "defer"}
    if (assessment.status in expected
            and bound.delivery.response_kind != expected[assessment.status]):
        issues.append("response_binding_delivery_mismatch")
    return issues


def enforce_binding(review, assignment, candidate):
    """Shared live/calibration gate; assessment never writes world/task state."""
    issues = binding_issues(review, assignment, candidate)
    if not issues:
        return review, issues
    checked = {**review, "accepted": False, "issues": [*review.get("issues", []),
        "当前候选与实际回应任务的绑定未通过（" + ", ".join(issues) + "）；"
        "仍需实际回应这条发言，或明确拒绝、暂缓、说明本人未知，原文片段和来源须对应。"]}
    return checked, issues
