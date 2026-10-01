"""Host-selected frozen release and player save catalogue; no client recipe input."""

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .experiment_initials import ExperimentStart, compatible_recipe_digests, digest, initial, start
from .storage import Conversation, ExperimentInitial, GameSave, Story


class NewGame(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=80)
    label: str = Field(default="新的旅程", min_length=1, max_length=80)


class GameSaves:
    def __init__(self, db, config, runtime, *, source_story_id):
        self.db, self.config, self.runtime = db, config, runtime
        self.source_story_id = source_story_id
        with db.session() as s:
            source = initial(s, source_story_id, config.workspace_owner)
            self.release_id = digest(source.recipe)
            self.compatible_release_ids = compatible_recipe_digests(source.recipe)
            self.title = source.recipe["title"]

    def owned(self, s, player_id):
        row = s.scalar(
            select(GameSave).where(
                GameSave.player_id == player_id,
                GameSave.owner_id == self.config.workspace_owner,
                GameSave.release_id == self.release_id,
            )
        )
        player = s.get(Conversation, player_id) if row else None
        story = s.get(Story, row.story_id) if row else None
        origin = s.get(ExperimentInitial, row.story_id) if row else None
        if (
            not row
            or not player
            or not story
            or not origin
            or player.owner_id != row.owner_id
            or story.owner_id != row.owner_id
            or player.story_id != row.story_id
            or not player.current_state.get("player")
            or origin.owner_id != row.owner_id
            or origin.receipt.get("source_story_id") != self.source_story_id
            or digest(origin.recipe) not in self.compatible_release_ids
        ):
            raise HTTPException(404, "存档不属于此作品。")
        return row, story

    def public(self, row, story):
        return {
            "id": row.player_id,
            "label": row.label,
            "created_at": row.created_at.isoformat(),
            "finished": bool(story.settings.get("narrative", {}).get("finished_at")),
        }

    def list(self):
        with self.db.session() as s:
            ids = s.scalars(
                select(GameSave.player_id)
                .where(
                    GameSave.owner_id == self.config.workspace_owner,
                    GameSave.release_id == self.release_id,
                )
                .order_by(GameSave.created_at.desc())
            ).all()
            return [self.public(*self.owned(s, player)) for player in ids]

    async def create(self, body: NewGame):
        await self.runtime.model_readiness.require()
        with self.db.session.begin() as s:
            source = initial(s, self.source_story_id, self.config.workspace_owner)
            if digest(source.recipe) != self.release_id:
                raise HTTPException(409, "作品初始设定发生变化，请重新载入发行版本。")
            previous = s.scalar(
                select(GameSave).where(
                    GameSave.owner_id == self.config.workspace_owner,
                    GameSave.release_id == self.release_id,
                    GameSave.request_id == body.request_id,
                )
            )
            if previous:
                if previous.label != body.label:
                    raise HTTPException(409, "同一新游戏请求不能更改存档名称。")
                return self.public(*self.owned(s, previous.player_id))
            # The namespaced request locks/receipt in start make retries atomic.
            request = "game:" + digest([self.release_id, body.request_id])
            receipt = start(
                s,
                ExperimentStart(request_id=request, source_story_id=self.source_story_id),
                self.config.workspace_owner,
                self.config,
                self.runtime,
                game_auto_backstage=True,
            )
            row = s.get(GameSave, receipt["player_id"])
            if row and row.label != body.label:
                raise HTTPException(409, "同一新游戏请求不能更改存档名称。")
            if not row:
                row = GameSave(
                    player_id=receipt["player_id"],
                    story_id=receipt["story_id"],
                    owner_id=self.config.workspace_owner,
                    release_id=self.release_id,
                    request_id=body.request_id,
                    label=body.label,
                )
                s.add(row)
                s.flush()
            return self.public(*self.owned(s, row.player_id))
