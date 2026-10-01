"""Author-only scene direction and bounded runtime plans, never world facts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from .role_policy import director_handoff_leak


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Beat(Contract):
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(max_length=160)
    objective: str = Field(max_length=2000)
    participants: list[str] = Field(max_length=20)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    completion: str = Field(max_length=2000)
    handoff: Literal["after_beat", "on_question", "continuous"] = "after_beat"
    max_segments: int = Field(default=3, ge=1, le=6)

    @model_validator(mode="after")
    def unique_people(self, info: ValidationInfo):
        if not (info.context or {}).get("authoring_draft") and (
            not self.title or not self.objective or not self.participants or not self.completion
        ):
            raise ValueError("互动段落须填写名称、目标、参与角色和完成条件。")
        if len(set(self.participants)) != len(self.participants):
            raise ValueError("互动段落的参与角色不能重复。")
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


class PerformanceJob(Contract):
    id: str = Field(min_length=1, max_length=80)
    actor: str = Field(min_length=1, max_length=80)
    addressees: list[str] = Field(default_factory=list, max_length=20)
    objective: str = Field(min_length=1, max_length=1000)
    delivery_constraints: list[str] = Field(default_factory=list, max_length=6)
    depends_on: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def bounded_delivery(self):
        if any(not value.strip() or len(value) > 300 for value in self.delivery_constraints):
            raise ValueError("表达约束须为非空且不超过300字的文本。")
        if any(director_handoff_leak(value) for value in [self.objective, *self.delivery_constraints]):
            raise ValueError("导演交还控制权是后台动作，不能写进演员任务。")
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
                raise ValueError("演出任务标识须唯一，依赖只能指向更早的任务。")
            if len(set(job.depends_on)) != len(job.depends_on):
                raise ValueError("演出任务依赖不能重复。")
            seen.add(job.id)
        return self


def validate_plan(plan: DirectorPlan, beat: Beat, eligible: set[str], visible_to: dict[str, set[str]]):
    """Reject the entire proposal; never silently widen an actor's audience."""
    if plan.beat_id != beat.id or len(plan.jobs) > beat.max_segments:
        raise ValueError("导演计划超出当前互动段落范围。")
    for job in plan.jobs:
        if job.actor not in set(beat.participants) & eligible:
            raise ValueError("导演安排了当前不可参与的角色。")
        if len(set(job.addressees)) != len(job.addressees) or not set(job.addressees) <= visible_to.get(
            job.actor, set()
        ):
            raise ValueError("回应对象不在该角色可感知范围内。")
    return plan


def ready_jobs(plan: DirectorPlan, approved: set[str], dispatched: set[str]):
    """Generation readiness only: approval does not mean a speech occurred."""
    return [j for j in plan.jobs if j.id not in dispatched and set(j.depends_on) <= approved]
