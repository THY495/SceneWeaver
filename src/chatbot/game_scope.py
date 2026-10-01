"""SQL scope for a host-bound release, including saves created after startup."""

from dataclasses import dataclass

from sqlalchemy import and_, or_, select

from .storage import Conversation, GameSave, Task


@dataclass(frozen=True)
class GameExecutionScope:
    owner_id: str
    release_id: str

    def stories(self):
        return select(GameSave.story_id).where(
            GameSave.owner_id == self.owner_id, GameSave.release_id == self.release_id
        )

    def actors(self):
        return select(Conversation.id).where(
            Conversation.owner_id == self.owner_id, Conversation.story_id.in_(self.stories())
        )

    def tasks(self):
        return and_(
            Task.owner_id == self.owner_id,
            or_(
                and_(
                    Task.kind == "connection_check",
                    Task.session_id.is_(None),
                    Task.payload["release_id"].as_string() == self.release_id,
                ),
                and_(
                    Task.kind.notin_(["scene_run", "npc_step", "connection_check"]),
                    Task.session_id.in_(self.actors()),
                ),
                and_(
                    Task.kind.in_(["scene_run", "npc_step"]),
                    Task.payload["story_id"].as_string().in_(self.stories()),
                    or_(Task.session_id.is_(None), Task.session_id.in_(self.actors())),
                ),
            ),
        )
