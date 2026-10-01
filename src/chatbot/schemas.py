from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .items.models import ItemCommand


class Route(BaseModel):
    route: Literal["direct", "retrieve", "complex"]
    reason: str = ""


class SearchPlan(BaseModel):
    queries: list[str] = Field(default_factory=list, max_length=3)
    sufficient: bool = False
    missing: list[str] = Field(default_factory=list, max_length=5)


class WorkItem(BaseModel):
    id: str
    question: str
    depends_on: list[str] = Field(default_factory=list, max_length=2)


class WorkPlan(BaseModel):
    tasks: list[WorkItem] = Field(min_length=1, max_length=2)


class MemoryCandidate(BaseModel):
    subject: str = Field(default="user", max_length=100)
    predicate: str = Field(max_length=100)
    value: str = Field(max_length=1500)
    scope: str = Field(default="general", max_length=100)
    category: Literal["preference", "personal_fact", "external_claim", "inference"]
    change: Literal["new", "update", "correction", "uncertain"] = "new"


class MemoryExtraction(BaseModel):
    candidates: list[MemoryCandidate] = Field(default_factory=list, max_length=5)


class ConstraintSource(BaseModel):
    message_id: str
    quote: str = Field(min_length=1, max_length=1000)


class ContextSummary(BaseModel):
    summary: str = Field(max_length=5000)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    open_items: list[str] = Field(default_factory=list, max_length=20)
    constraint_sources: list[ConstraintSource] = Field(default_factory=list, max_length=20)


class SubmitMessage(BaseModel):
    acknowledgement: bool = False
    content: str = Field(min_length=1, max_length=12000)
    client_message_id: str = Field(min_length=1, max_length=80)
    mode: Literal["auto", "direct", "retrieve", "complex"] = "auto"
    channel: Literal["public", "private"] = "public"
    addressees: list[str] = Field(default_factory=list, max_length=8)
    occurred_at: str | None = Field(default=None, max_length=100)


class MemoryEdit(BaseModel):
    value: str = Field(min_length=1, max_length=1500)
    scope: str = Field(default="general", max_length=100)


class CharacterRelationship(BaseModel):
    target: str = Field(min_length=1, max_length=100)
    relation: str = Field(min_length=1, max_length=100)
    status: Literal["current", "former", "uncertain"] = "current"
    period: str = Field(default="", max_length=100)
    basis: str = Field(default="", max_length=500)


class SessionProfile(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    core: str = Field(min_length=1, max_length=6000)
    document_ids: list[str] = Field(default_factory=list, max_length=30)
    world: str = Field(default="", max_length=2000)
    relationships: list[CharacterRelationship] = Field(default_factory=list, max_length=20)


class CharacterUpdate(SessionProfile):
    expected_version: int = Field(ge=1)


class CharacterStart(BaseModel):
    version: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=80)
    story_id: str | None = None


class CharacterImport(BaseModel):
    format: Literal["continuum.character.v1"]
    profile: SessionProfile


class SceneFact(BaseModel):
    key: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=1000)
    event_id: str = Field(min_length=1, max_length=100)


class EventRecipient(BaseModel):
    session_id: str
    learned_at: str = Field(min_length=1, max_length=100)
    basis: Literal["witnessed", "confirmed", "hearsay", "rumor"]
    source: str = Field(min_length=1, max_length=100)
    replaces_event_id: str | None = None


class StoryEventInput(BaseModel):
    request_id: str = Field(min_length=1, max_length=80)
    key: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=1000)
    occurred_at: str = Field(min_length=1, max_length=100)
    recipients: list[EventRecipient] = Field(min_length=1, max_length=20)


class PlayerStart(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class ClaimAssessment(BaseModel):
    text: str = Field(max_length=500, description="候选台词原文中的检查对象，不得摘录未进入台词的intent计划")
    basis: str = Field(max_length=350, description="先说明原始依据及其能证明的范围，不得以候选台词自身为依据")
    supported: bool
    reason_code: Literal[
        "none",
        "unsupported_fact",
        "invented_history",
        "acquisition_mismatch",
        "statement_promotion",
        "agreement_promotion",
        "player_agency",
        "mixed_recipient",
        "other",
    ] = Field(default="none", description="不支持时标记主要原因；无法分类用other，支持时用none")


class SpeechDelivery(BaseModel):
    remaining_request: str | None = Field(default=None, max_length=200)
    addressee_id: str | None = None
    expects_reply: bool = Field(
        default=False,
        description="最终台词是否向接收者提出仍需其回答的问题或明确请求；承认未知、自己稍后核实、确认收尾不算。不得从旧intent继承。",
    )
    response_kind: Literal["answer", "refuse", "defer", "silence"] = "answer"


class ActorReview(BaseModel):
    checks: list[ClaimAssessment] = Field(default_factory=list, max_length=12)
    accepted: bool
    issues: list[str] = Field(
        default_factory=list, max_length=6,
        description="仅列候选台词需要修正的缺陷；通过时为空，不填正面评价或未进入台词的计划风险",
    )
    delivery: SpeechDelivery


class RoleNote(BaseModel):
    kind: Literal["belief", "impression", "commitment", "goal", "relationship"]
    content: str = Field(min_length=1, max_length=800)
    source_id: str
    quote: str = Field(min_length=1, max_length=1000)
    same_as: str | None = Field(
        default=None, description="仅同种类、完全同义且状态未变的已有记忆ID；否则null"
    )


class RoleReflection(BaseModel):
    summary: str = Field(max_length=4000)
    notes: list[RoleNote] = Field(default_factory=list, max_length=5)


class AgreementReference(BaseModel):
    proposal_id: str
    version: int = Field(ge=1)
    item_id: str


class ObjectAction(BaseModel):
    object_id: str = Field(max_length=36)
    expected_version: int = Field(ge=1)
    verb: Literal["take", "put", "give"]
    target_id: str | None = None
    agreement: AgreementReference | None = None


class AgreementDecision(BaseModel):
    proposal_id: str
    expected_version: int = Field(ge=1)
    item_ids: list[str] = Field(min_length=1, max_length=8)
    decision: Literal["accept", "refuse", "withdraw"]


class SearchAction(BaseModel):
    object_id: str = Field(min_length=1, max_length=36)


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["co_location", "custody", "consent", "event", "tick"]
    actor_id: str | None = None
    event_id: str | None = None
    tick: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def parameters(self):
        required = {
            "co_location": "actor_id",
            "custody": "actor_id",
            "consent": "actor_id",
            "event": "event_id",
            "tick": "tick",
        }[self.kind]
        if getattr(self, required) is None:
            raise ValueError("条件缺少参数")
        if any(getattr(self, key) is not None for key in {"actor_id", "event_id", "tick"} - {required}):
            raise ValueError("条件包含多余参数")
        return self


class AgreementItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    executor_id: str
    action: ObjectAction
    depends_on: list[str] = Field(default_factory=list, max_length=8)
    conditions: list[Condition] = Field(default_factory=list, max_length=8)


class ProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=80)
    source_event_id: str
    proposal_id: str | None = None
    expected_version: int = Field(default=0, ge=0)
    items: list[AgreementItem] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def graph(self):
        graph = {item.key: item.depends_on for item in self.items}
        if len(graph) != len(self.items):
            raise ValueError("子事项 key 重复")
        visited, path = set(), set()

        def visit(key):
            if key not in graph or key in path:
                raise ValueError("依赖不存在或形成循环")
            if key in visited:
                return
            path.add(key)
            for other in graph[key]:
                visit(other)
            path.remove(key)
            visited.add(key)

        for key in graph:
            visit(key)
        return self


class ActorProposal(ProposalInput):
    request_id: str = "agent"


class ActorIntent(BaseModel):
    remaining_request: str = Field(default="", max_length=200)
    awaiting_player_action: bool = False
    act: Literal[
        "speak",
        "wait",
        "inspect",
        "object_action",
        "agreement_decision",
        "agreement_proposal",
        "search",
        "item_action",
    ] = "speak"
    purpose: str = Field(default="回应当前发言", max_length=300)
    addressee_id: str | None = None
    source_ids: list[str] = Field(default_factory=list, max_length=5)
    wait_for_player: bool = True
    deception: bool = False
    goal_id: str | None = None
    retrieval: Literal["context", "knowledge"] = "knowledge"
    trigger_reason: str = Field(default="", max_length=200)
    object_action: ObjectAction | None = None
    item_action: ItemCommand | None = None
    result_event_id: str | None = Field(
        default=None,
        max_length=36,
        description="明确选择：仅简述一次已观察物品操作的结果时填其事件ID；复杂、复合问题或非speak填null",
    )
    agreement_decision: AgreementDecision | None = None
    agreement_proposal: ActorProposal | None = None
    search_action: SearchAction | None = None
    expects_reply: bool = False
    reply_to: str | None = None
    response_kind: Literal["answer", "refuse", "defer", "silence"] = "answer"


class GroundedClaim(BaseModel):
    text: str = Field(
        min_length=1, max_length=500, description="从完整台词text逐字摘取的连续原句，不改写、不概括"
    )
    source_id: str
    quote: str = Field(min_length=1, max_length=1500)
    mode: Literal["fact", "reported", "belief", "hypothesis", "proposal", "commitment", "action"] = "fact"


class PerformanceSegment(BaseModel):
    text: str = Field(min_length=1, max_length=2400)
    expression: Literal["neutral", "happy", "sad", "angry", "surprised", "concerned"] = "neutral"
    pose: Literal["neutral", "arms_crossed", "thinking", "nodding"] = "neutral"
    voice_direction: str = Field(default="", max_length=160)


class ActorSpeech(BaseModel):
    text: str = Field(min_length=1, max_length=2400)
    spoken_parts: list[str] = Field(default_factory=list, max_length=12)
    claims: list[GroundedClaim] = Field(default_factory=list, max_length=8)
    performance: list[PerformanceSegment] = Field(default_factory=list, max_length=12)
