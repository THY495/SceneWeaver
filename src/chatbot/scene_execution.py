"""Deterministic opening execution, separate from the client's reading cursor."""

import re
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .observations import append_record
from .storage import Conversation, ExperimentInitial, Story, StoryEvent, Task, now, uid


def story_finished(story):
    return bool(story and story.settings.get("narrative", {}).get("finished_at"))


def finish_story(story):
    state = story.settings["narrative"]
    story.settings = {
        **story.settings,
        "narrative": {**state, "finished_at": state.get("finished_at") or now().isoformat()},
    }


def opening_active(story):
    return bool(
        story
        and story.settings.get("narrative", {}).get("execution", {}).get("phase") in {"opening", "video"}
    )


def projection(state, node):
    execution = state.get("execution")
    if not execution:
        return None  # Existing runs already committed their entire opening.
    return {
        **execution,
        "has_next": execution["phase"] == "video" or execution["index"] + 1 < len(node.get("opening") or []),
        "event_id": state["event_id"],
    }


def commit_block(s, story, node, block, actors):
    speaker = s.get(Conversation, actors[block["speaker"]]) if block.get("speaker") else None
    return append_record(
        s,
        story_id=story.id,
        kind="speech" if speaker else "host",
        actor=speaker,
        text=block["text"],
        observer_ids=[actors[a] for a in node["observers"]],
        channel="private",
        dedupe=f"narrative-node:{node['id']}"
        if not node.get("opening")
        else f"narrative-node:{node['id']}:{block['id']}",
        payload={
            **(
                {
                    "spoken_parts": [
                        part
                        for part in re.split(r"\([^)]*\)|（[^）]*）|\[[^\]]*\]|<[^>]*>", block["text"])
                        if part.strip()
                    ],
                    "voice_epochs": {
                        actors[a]: s.get(Conversation, actors[a]).epoch for a in node["observers"]
                    },
                }
                if speaker
                else {}
            ),
            **(
                {"key": f"narrative:{node['id']}:{block['id']}", "value": block["text"], "basis": "confirmed"}
                if not speaker
                else {}
            ),
            "source": "作者固定对白" if speaker else "作者剧情节点",
            "narrative_node": node["id"],
            "opening_block": block["id"],
        },
    )


class Advance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=80)
    node_id: str = Field(min_length=1, max_length=80)
    expected_version: int = Field(ge=1)
    video_result: Literal["ended", "skipped"] | None = None


def install_routes(router, db, who):
    @router.post("/stories/{sid}/narrative/advance")
    def advance(sid: str, body: Advance, identity=Depends(who)):
        from .experiment_initials import digest
        from .narrative import player_view

        with db.session.begin() as s:
            story = s.scalar(
                select(Story).where(Story.id == sid, Story.owner_id == identity).with_for_update()
            )
            if not story:
                raise HTTPException(404, "故事不存在。")
            key = "opening:" + digest(body.request_id)
            prior = s.scalar(
                select(StoryEvent).where(StoryEvent.story_id == sid, StoryEvent.request_id == key)
            )
            if prior:
                if prior.payload["request"] != body.model_dump(exclude_none=True):
                    raise HTTPException(409, "同一推进请求不能更改内容。")
                return prior.payload["receipt"]
            origin = s.get(ExperimentInitial, sid)
            state = story.settings.get("narrative", {})
            execution = state.get("execution", {})
            if not origin or not opening_active(story) or state.get("mode") == "backstage":
                raise HTTPException(409, "当前不在可推进的玩家开场。")
            if state["node"] != body.node_id or execution["version"] != body.expected_version:
                raise HTTPException(409, "场景执行进度已变化，请刷新。")
            if story.settings.get("paused"):
                raise HTTPException(409, "请先继续游玩，再推进下一段。")
            people = list(
                s.scalars(
                    select(Conversation)
                    .where(Conversation.story_id == sid)
                    .order_by(Conversation.id)
                    .with_for_update()
                )
            )
            ids = [c.id for c in people]
            if s.scalar(
                select(Task.id)
                .where(
                    Task.status.in_(["queued", "running", "waiting"]),
                    Task.kind.in_(["respond", "scene_run", "npc_step"]),
                    (Task.session_id.in_(ids)) | (Task.payload["story_id"].as_string() == sid),
                )
                .limit(1)
            ):
                raise HTTPException(409, "仍有运行任务，请等待结束后推进。")
            node = next(n for n in origin.recipe["narrative"]["nodes"] if n["id"] == state["node"])
            next_index = execution["index"] + 1
            updated = {**execution, "version": execution["version"] + 1}
            event_id = state["event_id"]
            if execution["phase"] == "video":
                if not body.video_result:
                    raise HTTPException(409, "请先完成视频或明确跳过。")
                if body.video_result == "skipped" and not node["video"].get("allow_skip", True):
                    raise HTTPException(409, "作者未允许跳过此片段。")
                block = (
                    node.get("opening") or [{"id": "legacy", "kind": "narration", "text": node["text"]}]
                )[0]
                event_id = commit_block(s, story, node, block, origin.receipt["actors"]).id
                updated.update(
                    index=0,
                    phase="opening"
                    if node.get("opening")
                    else "finished"
                    if node.get("ending")
                    else "interactive",
                    video_result=body.video_result,
                )
            elif body.video_result:
                raise HTTPException(409, "当前没有待完成的视频。")
            elif next_index < len(node.get("opening") or []):
                record = commit_block(s, story, node, node["opening"][next_index], origin.receipt["actors"])
                event_id = record.id
                updated["index"] = next_index
            else:
                updated["phase"] = "finished" if node.get("ending") else "interactive"
            story.settings = {
                **story.settings,
                "narrative": {**state, "execution": updated, "event_id": event_id},
            }
            if updated["phase"] == "finished":
                finish_story(story)
            for actor in people:
                actor.revision += 1
            receipt = player_view(story, origin.recipe)
            s.add(
                StoryEvent(
                    id=uid(),
                    story_id=sid,
                    request_id=key,
                    payload={"request": body.model_dump(exclude_none=True), "receipt": receipt},
                )
            )
            return receipt
