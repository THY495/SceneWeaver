"""Diagnostic-only recovery input; never exports actor evidence or review prose."""

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import Field
from sqlalchemy import select

from .directing import Contract, PerformanceJob
from .director_state import ack_only_world_advance
from .storage import Conversation, Operation, Story


class RejectedPremise(Contract):
    text: str = Field(min_length=1, max_length=1000)
    premise_kind: (
        Literal[
            "world_fact",
            "past_action",
            "past_inaction",
            "acquisition",
            "current_uncertainty",
            "reported_statement",
            "expression",
        ]
        | None
    ) = None
    code: Literal["unsupported_premise"] = "unsupported_premise"


class RecoveryNotice(Contract):
    actor: str = Field(min_length=1, max_length=80)
    job_id: str = Field(min_length=1, max_length=80)
    rejected_premises: list[RejectedPremise] = Field(min_length=1, max_length=4)


def original_premises(job, checks):
    """Use exact, nonblank substrings of one original field, never basis/issues."""
    fields = [job.objective, *job.delivery_constraints]
    result = []
    for check in checks:
        try:
            item = RejectedPremise.model_validate(check)
        except ValueError:
            continue
        if item.text.strip() and any(item.text in field for field in fields) and item not in result:
            result.append(item)
        if len(result) == 4:
            break
    return tuple(result)


def job_hash(job):
    return hashlib.sha256(
        json.dumps(job.model_dump(), ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


@dataclass(frozen=True)
class BoundRecoveryNotice:
    task_id: str
    lease_version: int
    plan_revision: int
    job_digest: str
    actor_revision: int
    privacy_version: int
    world_seq: int
    notice: RecoveryNotice

    def current(self, s, task, turn, ledger, *, lock_actor=False):
        if (
            task.id != self.task_id
            or task.lease_version != self.lease_version
            or not ledger
            or ledger.get("revision") != self.plan_revision
            or ledger.get("candidates", {}).get(self.notice.job_id, {}).get("status") == "committed"
        ):
            return False
        source = next(
            (j for j in ledger.get("plan", {}).get("jobs", []) if j["id"] == self.notice.job_id), None
        )
        if not source:
            return False
        try:
            job = PerformanceJob.model_validate(source)
            notice = RecoveryNotice.model_validate(self.notice.model_dump())
        except ValueError:
            return False
        if job.actor != self.notice.actor or job_hash(job) != self.job_digest:
            return False
        # Validate the excerpt again at the boundary, including mutable model values.
        if original_premises(job, [p.model_dump() for p in notice.rejected_premises]) != tuple(
            notice.rejected_premises
        ):
            return False
        query = select(Conversation).where(Conversation.id == self.notice.actor)
        actor = s.scalar(query.with_for_update() if lock_actor else query)
        story = s.get(Story, turn.story_id)
        return bool(
            actor
            and actor.story_id == turn.story_id
            and actor.revision == self.actor_revision
            and actor.privacy_version == self.privacy_version
            and story
            and story.owner_id == task.owner_id
            and (
                story.seq == self.world_seq
                or ack_only_world_advance(s, task, turn.story_id, self.world_seq, story.seq)
            )
        )

    def installed_current(self, s, task, turn, *, lock_actor=False):
        row = s.get(Operation, f"{task.id}:director-ledger")
        return self.current(s, task, turn, row.payload if row else None, lock_actor=lock_actor)


def bind_notice(task, ledger, job, actor_revision, privacy_version, world_seq, checks):
    premises = original_premises(job, checks)
    if not premises:
        return None
    return BoundRecoveryNotice(
        task.id,
        task.lease_version,
        ledger["revision"],
        job_hash(job),
        actor_revision,
        privacy_version,
        world_seq,
        RecoveryNotice(actor=job.actor, job_id=job.id, rejected_premises=list(premises)),
    )
