"""Explicit execution scope for both player turns and offscreen NPC activity."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Settings
    from ..context import ContextManager
    from ..knowledge import Knowledge
    from ..providers import Provider
    from ..runtime import Runtime
    from ..storage import Database


@dataclass(frozen=True)
class TurnContext:
    story_id: str
    player_id: str | None
    actor_ids: tuple[str, ...]

    def visible_cast(self, info: dict) -> list[dict]:
        """Intersect the task scope with this actor's already-filtered perception."""
        allowed = {self.player_id, *self.actor_ids}
        return [person for person in info["perceived_cast"] if person["id"] in allowed]


@dataclass(frozen=True)
class NPCServices:
    """Dependencies available to NPC stages; no office-agent orchestration methods."""

    db: "Database"
    config: "Settings"
    runtime: "Runtime"
    provider: "Provider"
    context: "ContextManager"
    knowledge: "Knowledge"
    voice: object | None = None
