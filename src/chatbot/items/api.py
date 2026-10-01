"""Authoring-only, stateless previews. Never reads or writes a live story."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from .catalog import catalog, instantiate
from .models import Key, Placement, StrictModel, World
from .rules import RuleViolation, preview_container_state, preview_move, validate_inventory


class PresetRequest(StrictModel):
    preset_id: Key
    instance_id: Key
    placement: Placement


class MovePreview(StrictModel):
    world: World
    actor_id: Key
    item_id: Key
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    destination: Placement


class ContainerPreview(StrictModel):
    world: World
    actor_id: Key
    item_id: Key
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    action: Literal["open", "close"]


def authoring_router(owner):
    router = APIRouter(prefix="/api/role/authoring/items", dependencies=[Depends(owner)])

    @router.get("/presets")
    def presets():
        return catalog()

    @router.post("/instantiate")
    def instance(body: PresetRequest):
        try:
            return instantiate(body.preset_id, body.instance_id, body.placement)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/validate")
    def validate(world: World):
        try:
            validate_inventory(world)
        except RuleViolation as exc:
            return {"valid": False, "code": exc.code, "reason": str(exc)}
        return {"valid": True}

    @router.post("/preview-move")
    def preview(body: MovePreview):
        try:
            validate_inventory(body.world)
            candidate = preview_move(
                body.world, body.actor_id, body.item_id, body.expected_revision, body.destination
            )
        except RuleViolation as exc:
            return {"allowed": False, "code": exc.code, "reason": str(exc)}
        return {"allowed": True, "preview_only": True, "world": candidate.model_dump()}

    @router.post("/preview-container")
    def container_preview(body: ContainerPreview):
        try:
            validate_inventory(body.world)
            candidate = preview_container_state(
                body.world, body.actor_id, body.item_id, body.expected_revision, opened=body.action == "open"
            )
        except RuleViolation as exc:
            return {"allowed": False, "code": exc.code, "reason": str(exc)}
        return {"allowed": True, "preview_only": True, "world": candidate.model_dump()}

    return router
