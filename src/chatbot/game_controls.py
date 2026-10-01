"""Small player controls; never accept creator SceneSettings or expose task payloads."""

import base64
import re

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy import select

from .scene_control import invalidate_scene
from .scene_execution import story_finished
from .storage import Conversation, Story, Task, now


class Pause(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: StrictBool


def install_controls(app, db, config, player_id, scope):
    dependencies = [Depends(app.state.owner_dependency), Depends(scope)]

    @app.get("/api/game/session", dependencies=dependencies)
    def session():
        with db.session() as s:
            player = s.get(Conversation, player_id)
            tasks = s.scalars(
                select(Task)
                .where(
                    Task.session_id == player_id,
                    Task.kind == "respond",
                    Task.epoch == player.epoch,
                )
                .order_by(Task.created_at.desc())
                .limit(10)
            )
            return {
                "id": player.id,
                "title": s.get(Story, player.story_id).title,
                "story_id": player.story_id,
                "epoch": player.epoch,
                "tasks": [
                    {
                        "id": t.id,
                        "status": t.status,
                        "epoch": t.epoch,
                        "stop_reason": (t.result or {}).get("stop_reason"),
                    }
                    for t in tasks
                ],
            }

    @app.get("/api/game/assets/{key}", dependencies=dependencies)
    def asset(key: str):
        from .player_snapshot import player_snapshot
        from .presentation import asset_dir
        from .role_api import SceneSettings

        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise HTTPException(404)
        with db.session() as s:
            s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            player = s.get(Conversation, player_id)
            story = s.get(Story, player.story_id)
            settings = SceneSettings(
                **{k: v for k, v in story.settings.items() if k in SceneSettings.model_fields}
            )
            snapshot = player_snapshot(s, player, story, settings.model_dump())

        def sprites(value):
            if isinstance(value, dict):
                for field, child in value.items():
                    if field in {"sprite", "base_sprite", "background"} and isinstance(child, str):
                        yield child
                    elif isinstance(child, dict | list):
                        yield from sprites(child)
            elif isinstance(value, list):
                for child in value:
                    yield from sprites(child)

        if key not in set(sprites(snapshot)):
            raise HTTPException(404)
        path = asset_dir(config, config.workspace_owner) / (key + ".png")
        if not path.is_file():
            raise HTTPException(404)
        return {"data_url": "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()}

    @app.put("/api/game/pause", dependencies=dependencies)
    async def pause(body: Pause):
        if not body.paused:
            await app.state.runtime.model_readiness.require()
        with db.session.begin() as s:
            player = s.get(Conversation, player_id)
            story = s.scalar(
                select(Story)
                .where(
                    Story.id == player.story_id,
                    Story.owner_id == config.workspace_owner,
                )
                .with_for_update()
            )
            if not story:
                raise HTTPException(404)
            if not body.paused and (
                story_finished(story) or story.settings.get("narrative", {}).get("mode") == "backstage"
            ):
                raise HTTPException(409, "当前场景不能由玩家继续。")
            was_paused = bool(story.settings.get("paused"))
            if was_paused != body.paused:
                story.settings = {**story.settings, "paused": body.paused}
                invalidate_scene(s, story.id, app.state.runtime)
                if was_paused and not body.paused:
                    from .role_api import SceneSettings

                    cooldown = SceneSettings(**story.settings).cooldown_seconds
                    player.current_state = {
                        **player.current_state,
                        "resume_grace_until": now().timestamp() + cooldown,
                    }
            return {"paused": body.paused}

    @app.post("/api/game/backstage/retry", dependencies=dependencies)
    def retry_backstage():
        from .game_backstage import MAX_STEPS

        with db.session.begin() as s:
            player = s.get(Conversation, player_id)
            story = s.scalar(
                select(Story)
                .where(Story.id == player.story_id, Story.owner_id == config.workspace_owner)
                .with_for_update()
            )
            state = story.settings.get("narrative", {}) if story else {}
            progress = story.settings.get("game_backstage_progress", {}) if story else {}
            if (
                state.get("mode") != "backstage"
                or state.get("node") not in story.settings.get("game_backstage_plan", {}).get("directed", [])
                or progress.get("entry_event_id") != state.get("event_id")
            ):
                raise HTTPException(409, "当前没有可恢复的幕后演出。")
            if progress.get("status") == "running":
                return {"status": "running"}
            if progress.get("status") != "error" or progress.get("issued", 0) >= MAX_STEPS:
                raise HTTPException(409, "幕后演出已达到自动执行上限。")
            story.settings = {
                **story.settings,
                "game_backstage_progress": {
                    **progress,
                    "last_task": None,
                    "status": "running",
                },
            }
            return {"status": "running"}
