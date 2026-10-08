"""Author-only scene direction and bounded runtime plans, never world facts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator
from pydantic_core import PydanticCustomError

from .role_policy import director_handoff_leak


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Beat(Contract):
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(max_length=160)
    objective: str = Field(max_length=2000)
    participants: list[str] = Field(max_length=20)
    role_focus: dict[str, str] = Field(default_factory=dict, max_length=20)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    completion: str = Field(max_length=2000)
    handoff: Literal["after_beat", "on_question", "continuous"] = "after_beat"
    max_segments: int = Field(default=3, ge=1, le=6)
    npc_exchange_required: bool = False
    # Author opts into source-only tasks; existing prose objectives do not grant this.
    source_response_tasks: bool = False
    # Explicit author permission for a single context-grounded reply; never inferred from prose.
    direct_response_actor: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def unique_people(self, info: ValidationInfo):
        if not (info.context or {}).get("authoring_draft") and (
            not self.title or not self.objective or not self.participants or not self.completion
        ):
            raise ValueError("互动段落须填写名称、目标、参与角色和完成条件。")
        if len(set(self.participants)) != len(self.participants):
            raise ValueError("互动段落的参与角色不能重复。")
        if (
            self.npc_exchange_required
            and len(self.participants) < 2
            and not (info.context or {}).get("authoring_draft")
        ):
            raise ValueError("要求NPC交流的段落须选择至少两位参与角色。")
        if not set(self.role_focus) <= set(self.participants):
            raise ValueError("角色关注点只能填写给本段参与角色。")
        if self.direct_response_actor and self.direct_response_actor not in self.participants:
            raise ValueError("直接回应角色须为本段参与角色。")
        if any(not focus.strip() or len(focus) > 500 for focus in self.role_focus.values()):
            raise ValueError("角色关注点须为非空且不超过500字的文本。")
        if any(not x.strip() or len(x) > 2000 for x in self.constraints):
            raise ValueError("演出约束须为非空且不超过2000字的文本。")
        return self


class SceneDirection(Contract):
    version: Literal[1] = 1
    objective: str = Field(max_length=2000)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    beats: list[Beat] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def unique_beats(self, info: ValidationInfo):
        if not self.objective and not (info.context or {}).get("authoring_draft"):
            raise ValueError("请填写本场戏的目标。")
        if len({b.id for b in self.beats}) != len(self.beats):
            raise ValueError("互动段落标识不能重复。")
        if any(not x.strip() or len(x) > 2000 for x in self.constraints):
            raise ValueError("场景约束须为非空且不超过2000字的文本。")
        return self


class GroundedResponse(Contract):
    """The runtime supplies the actual utterance; no free premise or stance."""

    event_id: str | None = Field(default=None, min_length=1, max_length=80)
    job_id: str | None = Field(default=None, min_length=1, max_length=80)
    focus_quote: str | None = Field(default=None, min_length=1, max_length=1000)
    delivery: Literal["natural", "brief"] = "natural"
    purpose: Literal["respond", "invite_player"] = "respond"

    @model_validator(mode="after")
    def one_source(self):
        if bool(self.event_id) == bool(self.job_id):
            raise PydanticCustomError('director_source_exclusive', "结构化回应须且只能引用真实事件或前序任务之一。")
        if self.job_id and self.focus_quote:
            raise PydanticCustomError('director_future_quote', "前序任务尚未发表，不能预写其具体台词片段。")
        if self.purpose == "invite_player" and self.job_id:
            raise PydanticCustomError('director_inquiry_source', "开放邀请须绑定当前玩家已发表的原话，不能引用未来任务。")
        return self


class PerformanceJob(Contract):
    id: str = Field(min_length=1, max_length=80)
    actor: str = Field(min_length=1, max_length=80)
    addressees: list[str] = Field(default_factory=list, max_length=20)
    objective: str = Field(default="", max_length=1000)
    grounded_response: GroundedResponse | None = None
    delivery_constraints: list[str] = Field(default_factory=list, max_length=6)
    depends_on: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def bounded_delivery(self):
        if self.grounded_response:
            if self.objective or self.delivery_constraints:
                raise PydanticCustomError('director_task_conflict', "结构化回应不能夹带自由任务目标或表达约束。")
            if self.grounded_response.job_id and self.grounded_response.job_id not in self.depends_on:
                raise PydanticCustomError('director_source_dependency', "结构化回应须依赖引用的前序任务。")
        elif not self.objective:
            raise PydanticCustomError('director_task_missing', "演员任务须有自由目标或结构化回应。")
        if any(not value.strip() or len(value) > 300 for value in self.delivery_constraints):
            raise PydanticCustomError('director_delivery_invalid', "表达约束须为非空且不超过300字的文本。")
        if any(director_handoff_leak(value) for value in [self.objective, *self.delivery_constraints]):
            raise PydanticCustomError('director_control_leak', "导演交还控制权是后台动作，不能写进演员任务。")
        return self


class DirectorPlan(Contract):
    version: Literal[1] = 1
    beat_id: str = Field(min_length=1, max_length=80)
    jobs: list[PerformanceJob] = Field(default_factory=list, max_length=6)
    handoff: bool = True
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def ordered_dependencies(self):
        # Array order is presentation order; dependencies must point backwards.
        seen = set()
        for job in self.jobs:
            if job.id in seen or not set(job.depends_on) <= seen:
                raise PydanticCustomError('director_dependency_order', "演出任务标识须唯一，依赖只能指向更早的任务。")
            if len(set(job.depends_on)) != len(job.depends_on):
                raise PydanticCustomError('director_dependency_duplicate', "演出任务依赖不能重复。")
            seen.add(job.id)
        return self


def validate_plan(
    plan: DirectorPlan,
    beat: Beat,
    eligible: set[str],
    visible_to: dict[str, set[str]],
    *,
    npc_exchange_observed=False,
):
    """Reject the entire proposal; never silently widen an actor's audience."""
    if plan.beat_id != beat.id or len(plan.jobs) > beat.max_segments:
        raise ValueError("导演计划超出当前互动段落范围。")
    for job in plan.jobs:
        if job.actor not in set(beat.participants) & eligible:
            raise ValueError("导演安排了当前不可参与的角色。")
        if len(job.addressees) > 1:
            raise ValueError("每段导演任务只能指定一位主要接收者；其他在场人物可以旁听。")
        if len(set(job.addressees)) != len(job.addressees) or not set(job.addressees) <= visible_to.get(
            job.actor, set()
        ):
            raise ValueError("回应对象不在该角色可感知范围内。")
    if (
        beat.npc_exchange_required
        and plan.jobs
        and not npc_exchange_observed
        and not any(
            target in set(beat.participants) & eligible and target != job.actor
            for job in plan.jobs
            for target in job.addressees
        )
    ):
        raise ValueError("作者要求NPC之间直接交流，本计划却全部面向玩家或没有NPC接收者。")
    return plan


def ready_jobs(plan: DirectorPlan, approved: set[str], dispatched: set[str]):
    """Generation readiness only: approval does not mean a speech occurred."""
    return [j for j in plan.jobs if j.id not in dispatched and set(j.depends_on) <= approved]
