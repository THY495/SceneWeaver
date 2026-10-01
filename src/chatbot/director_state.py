"""Private, task-scoped director ledger. Never writes speech or role memories."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass

from sqlalchemy import select

from .directing import DirectorPlan
from .runtime import StaleTask
from .storage import Conversation, Operation, SceneRecord, Story, Task, now


def ack_only_world_advance(s, task, story_id, before_seq, after_seq):
    """A quick acknowledgement is the sole tolerated concurrent world change."""
    if not before_seq < after_seq <= before_seq + 2:
        return False
    records = list(
        s.scalars(
            select(SceneRecord).where(
                SceneRecord.story_id == story_id,
                SceneRecord.seq > before_seq,
                SceneRecord.seq <= after_seq,
            )
        )
    )
    return bool(records) and all(
        r.task_id == task.id
        and r.kind in {"ack_candidate", "speech"}
        and r.payload.get("acknowledgement") is True
        for r in records
    )


def rebase_ack_plan(s, task, story, before_seq):
    """Keep an already installed plan current when its own filler is recorded."""
    row = s.get(Operation, f"{task.id}:director-ledger")
    if row and row.payload.get("world_seq") == before_seq:
        data = deepcopy(row.payload)
        data["world_seq"] = story.seq
        row.payload = data


@contextmanager
def locked_plan(db, task, story_id, world_seq, *, allow_ack_advance=False):
    # Match world publication lock order. Do not hold these locks across models.
    with db.session.begin() as s:
        story = s.scalar(select(Story).where(Story.id == story_id).with_for_update())
        live = s.scalar(select(Task).where(Task.id == task.id).with_for_update())
        if (
            not story
            or story.owner_id != task.owner_id
            or (
                story.seq != world_seq
                and not (
                    allow_ack_advance
                    and ack_only_world_advance(s, task, story_id, world_seq, story.seq)
                )
            )
            or not live
            or live.owner_id != story.owner_id
            or live.status != "running"
            or live.lease_version != task.lease_version
            or not live.lease_until
            or live.lease_until <= now()
            or live.deadline <= now()
        ):
            raise StaleTask("导演任务或世界版本已失效。")
        if live.kind == "respond":
            player = s.get(Conversation, live.session_id)
            if not player or player.story_id != story_id or player.epoch != live.epoch:
                raise StaleTask("玩家输入已变化。")
        elif live.kind in {"scene_run", "npc_step"}:
            from .scene_tasks import valid_scene

            if live.payload.get("story_id") != story_id or not valid_scene(s, live, story):
                raise StaleTask("场景活动已失效。")
        else:
            raise StaleTask("任务类型不支持导演演出。")
        key = f"{task.id}:director-ledger"
        yield s, key, s.get(Operation, key)


def install_plan(
    db,
    task,
    story_id,
    world_seq,
    plan: DirectorPlan,
    *,
    expected_revision=0,
    validate_scope=None,
    recovery_reason="",
    allow_ack_advance=False,
):
    """CAS replacement; rejected stale writers cannot overwrite a newer plan."""
    plan = DirectorPlan.model_validate(plan.model_dump())
    with locked_plan(db, task, story_id, world_seq, allow_ack_advance=allow_ack_advance) as (s, key, row):
        world_seq = s.get(Story, story_id).seq
        if validate_scope is not None and not validate_scope(s):
            raise StaleTask("导演规划范围在安装前已变化。")
        previous = row.payload if row else {}
        if recovery_reason not in {"", "assignment_rejected", "draft_rejected", "state_changed"}:
            raise ValueError("未知的导演重规划原因。")
        recovery_count = previous.get("recovery_count", 0) + bool(recovery_reason)
        if recovery_count > 1:
            raise StaleTask("本轮导演重规划次数已用完。")
        if previous.get("revision", 0) != expected_revision:
            raise StaleTask("导演计划已被替换。")
        prefix = f"task:{task.id}:"
        published = list(
            s.scalars(
                select(SceneRecord.dedupe).where(
                    SceneRecord.story_id == story_id, SceneRecord.dedupe.startswith(prefix)
                )
            )
        )
        steps = sorted(int(key[len(prefix) :]) for key in published if key[len(prefix) :].isdigit())
        if steps != list(range(len(steps))):
            raise StaleTask("已发布演出序号不连续，不能安装新计划。")
        payload = {
            "version": 1,
            "revision": expected_revision + 1,
            "recovery_count": recovery_count,
            "recovery_reason": recovery_reason,
            "story_id": story_id,
            "world_seq": world_seq,
            "epoch": task.epoch,
            "step_offset": len(steps),
            "plan": plan.model_dump(),
            "candidates": {},
        }
        if row:
            archived = deepcopy(previous)
            for candidate in archived["candidates"].values():
                if candidate["status"] != "committed":
                    candidate["status"] = "expired"
                    candidate.pop("value", None)
            s.add(Operation(id=f"{key}:history:{expected_revision}", task_id=task.id, payload=archived))
            row.payload = payload
        else:
            s.add(Operation(id=key, task_id=task.id, payload=payload))
        return deepcopy(payload)


def save_candidate(
    db,
    task,
    story_id,
    world_seq,
    *,
    plan_revision,
    job_id,
    expected_revision,
    dependency_revisions: dict[str, int],
    candidate: dict,
):
    """Save approved private actor output, invalidating transitive descendants.

    Caller performs semantic review first. Approval here grants no publication
    rights: final world commit must recheck actor permissions and audience.
    """
    with locked_plan(db, task, story_id, world_seq, allow_ack_advance=True) as (s, _, row):
        world_seq = s.get(Story, story_id).seq
        if not row or row.payload["revision"] != plan_revision:
            raise StaleTask("导演计划已失效。")
        data = deepcopy(row.payload)
        if data["world_seq"] != world_seq:
            raise StaleTask("候选所用世界版本已失效。")
        plan = DirectorPlan.model_validate(data["plan"])
        jobs = {job.id: job for job in plan.jobs}
        job = jobs.get(job_id)
        if not job or set(dependency_revisions) != set(job.depends_on):
            raise ValueError("候选依赖与导演计划不符。")
        candidates = data["candidates"]
        old = candidates.get(job_id, {})
        if old.get("revision", 0) != expected_revision:
            raise StaleTask("候选已被修订。")
        for dep, revision in dependency_revisions.items():
            source = candidates.get(dep, {})
            if source.get("status") not in {"approved", "committed"} or source.get("revision") != revision:
                raise StaleTask("候选引用了失效的前句。")
        if old.get("status") == "committed":
            raise StaleTask("已演出的候选不可修订。")
        affected = {job_id}
        for following in plan.jobs:
            if set(following.depends_on) & affected:
                affected.add(following.id)
                if following.id in candidates:
                    candidates[following.id]["status"] = "expired"
                    # Keep revision tombstones but discard unpublished content.
                    candidates[following.id].pop("value", None)
        candidates[job_id] = {
            "revision": expected_revision + 1,
            "status": "approved",
            "dependency_revisions": dict(dependency_revisions),
            "value": deepcopy(candidate),
        }
        row.payload = data
        return deepcopy(candidates[job_id])


@dataclass(frozen=True)
class CandidateCommit:
    plan_revision: int
    job_id: str
    candidate_revision: int


def check_publication(s, task, story, token: CandidateCommit, *, step, actor_id, value, event_id=None):
    """Called within the existing world commit transaction with Story locked."""
    row = s.get(Operation, f"{task.id}:director-ledger")
    if event_id and row and row.payload["revision"] != token.plan_revision:
        row = s.get(Operation, f"{task.id}:director-ledger:history:{token.plan_revision}")
    if not row or row.payload["revision"] != token.plan_revision:
        raise StaleTask("导演计划已被替换。")
    data = deepcopy(row.payload)
    plan = DirectorPlan.model_validate(data["plan"])
    candidates = data["candidates"]
    candidate = candidates.get(token.job_id, {})
    index = step - data.get("step_offset", 0)
    if (
        data["story_id"] != story.id
        or data["epoch"] != task.epoch
        or index < 0
        or index >= len(plan.jobs)
        or plan.jobs[index].id != token.job_id
        or plan.jobs[index].actor != actor_id
        or candidate.get("revision") != token.candidate_revision
        or candidate.get("value") != value
    ):
        raise StaleTask("发布内容或身份与已审查候选不符。")
    if event_id:
        if candidate.get("status") != "committed" or candidate.get("event_id") != event_id:
            raise StaleTask("重复提交与原演出记录不符。")
        return row, data
    if data["world_seq"] != story.seq or candidate.get("status") != "approved":
        raise StaleTask("候选或世界状态已失效。")
    if any(candidates.get(job.id, {}).get("status") != "committed" for job in plan.jobs[:index]):
        raise StaleTask("前序演出尚未正式发生。")
    for dep, revision in candidate["dependency_revisions"].items():
        source = candidates.get(dep, {})
        if source.get("status") != "committed" or source.get("revision") != revision:
            raise StaleTask("依赖前句尚未演出或已变化。")
    return row, data


def mark_publication(row, data, token: CandidateCommit, story, event_id, *, world_action=False):
    """Advance only for events committed atomically by this publication."""
    candidate = data["candidates"][token.job_id]
    candidate.update(status="committed", event_id=event_id)
    data["world_seq"] = story.seq
    if world_action:
        for pending in data["candidates"].values():
            if pending["status"] == "approved":
                pending["status"] = "expired"
                pending.pop("value", None)
    row.payload = data
