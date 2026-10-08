"""Evidence-bound beat progress. Completing a plan does not complete a beat."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ..auto_routing import Evidence
from ..director_state import locked_plan
from ..runtime import StaleTask
from ..storage import Operation, SceneRecord, Story
from .director import snapshot
from .exchange_requirement import beat_committed_event_ids, npc_exchange_observed


class BeatAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["met", "not_met", "uncertain"]
    evidence: list[Evidence] = Field(default_factory=list, max_length=8)
    reason: str = Field(min_length=1, max_length=1000)


async def assess_beat(services, task, turn, beat_id):
    key = f"{task.id}:beat-assessment:" + hashlib.sha256(beat_id.encode()).hexdigest()[:24]
    with services.db.session() as s:
        saved = s.get(Operation, key)
        if saved:
            return saved.payload
        frozen = snapshot(s, task, turn, beat_id=beat_id)
        if frozen is None or frozen.finished:
            return {"advanced": False, "reason": "没有待评估互动段落。"}
        events = set(s.scalars(select(SceneRecord.id).where(SceneRecord.task_id == task.id)))
        if task.payload.get("event_id"):
            events.add(task.payload["event_id"])
        exchange_events = beat_committed_event_ids(s, task.id, beat_id)
    beat = next(b for b in frozen.direction.beats if b.id == beat_id)
    observations = json.loads(frozen.public_context)["events"]
    exchange_met = not beat.npc_exchange_required or npc_exchange_observed(beat, observations, exchange_events)
    messages = [
        {
            "role": "system",
            "content": "判断当前互动段落的完成条件。只依据已发生事件，计划和目标不代表完成。"
            "言论仅证明角色说过，不自动证明其中事实；玩家的选择必须有玩家实际行动或发言支持。"
            "满足时引用事件ID与逐字原文，至少一条本轮事件。无法判断则uncertain；不强行推进。",
        },
        {
            "role": "user",
            "content": json.dumps(
                {"completion": beat.completion, "events": observations, "current_event_ids": sorted(events)},
                ensure_ascii=False,
            ),
        },
    ]
    assessment = BeatAssessment.model_validate(
        await services.provider.chat(
            task.id,
            "director_progress",
            messages,
            schema=BeatAssessment,
            max_tokens=700,
            operation=f"{key}:{frozen.world_seq}",
        )
    )
    sources = {e["event_id"]: e["text"] for e in observations}
    advanced = (
        exchange_met
        and assessment.status == "met"
        and bool(assessment.evidence)
        and all(e.event_id in sources and e.quote in sources[e.event_id] for e in assessment.evidence)
        and any(e.event_id in events for e in assessment.evidence)
    )
    result = {
        "advanced": advanced,
        "assessment": assessment.model_dump(),
        "beat_id": beat_id,
        "npc_exchange_met": exchange_met if beat.npc_exchange_required else None,
    }
    with locked_plan(services.db, task, turn.story_id, frozen.world_seq) as (s, _, _):
        existing = s.get(Operation, key)
        if existing:
            return existing.payload
        if snapshot(s, task, turn, beat_id=beat_id) != frozen:
            raise StaleTask("段落评估期间场景已变化。")
        if advanced:
            story = s.get(Story, turn.story_id)
            state = story.settings["narrative"]
            prior = state.get("direction_progress", {})
            completed = (
                prior.get("completed", []) if prior.get("entry_event_id") == frozen.entry_event_id else []
            )
            story.settings = {
                **story.settings,
                "narrative": {
                    **state,
                    "direction_progress": {
                        "entry_event_id": frozen.entry_event_id,
                        "completed": list(dict.fromkeys([*completed, beat_id])),
                    },
                },
            }
        s.add(Operation(id=key, task_id=task.id, payload=result))
    return result
