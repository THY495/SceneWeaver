"""Player UI bookmarks, isolated from narrative facts and NPC memory."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from .storage import Conversation, PlayerReading, Story


class ReadingProgress(BaseModel):
    version: Literal[1]
    entry: str = Field(max_length=80)
    selected: str = Field(min_length=1, max_length=100)
    acknowledged: bool
    fingerprint: str = Field(pattern=r"^[0-9a-f]{1,8}$")


class ReadingWrite(BaseModel):
    epoch: int = Field(ge=0)
    expected_revision: int = Field(ge=0)
    progress: ReadingProgress


def reading_router(db, who):
    # Reading epoch means privacy generation, NOT task epoch (which changes
    # on every player input). Fresh runs already have distinct session IDs.
    router = APIRouter()

    def player(s, aid, identity):
        # Serialize creation and updates without mutating session revision/epoch.
        row = s.scalar(
            select(Conversation)
            .where(Conversation.id == aid, Conversation.owner_id == identity)
            .with_for_update()
        )
        if not row or not row.story_id or not row.current_state.get("player"):
            raise HTTPException(404, "玩家会话不存在。")
        world = s.get(Story, row.story_id)
        if not world or world.owner_id != identity:
            raise HTTPException(404, "故事不存在。")
        return row

    @router.get("/players/{aid}/reading")
    def read(aid: str, identity=Depends(who)):
        with db.session.begin() as s:
            row = player(s, aid, identity)
            saved = s.get(PlayerReading, (aid, row.privacy_version))
            return {
                "epoch": row.privacy_version,
                "revision": saved.revision if saved else 0,
                "progress": saved.progress if saved else None,
            }

    @router.put("/players/{aid}/reading")
    def write(aid: str, body: ReadingWrite, identity=Depends(who)):
        with db.session.begin() as s:
            row = player(s, aid, identity)
            if body.epoch != row.privacy_version:
                raise HTTPException(409, "运行已重置，请重新进入试玩。")
            saved = s.get(PlayerReading, (aid, row.privacy_version))
            revision = saved.revision if saved else 0
            value = body.progress.model_dump()
            # A lost response can be retried safely, but never overwrite a newer bookmark.
            if saved and revision == body.expected_revision + 1 and saved.progress == value:
                return {"revision": revision}
            if revision != body.expected_revision:
                raise HTTPException(409, "阅读位置已由其他窗口更新，请重新进入试玩。")
            if not saved:
                saved = PlayerReading(session_id=aid, epoch=row.privacy_version)
                s.add(saved)
            saved.progress, saved.revision = value, revision + 1
            return {"revision": saved.revision}

    return router
