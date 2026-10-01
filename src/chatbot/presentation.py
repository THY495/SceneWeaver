"""Presentation assets and player-scoped stage, separate from model context."""

import base64
import hashlib
import re
import struct

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select

from .observations import perceive, perceived_cast, visible_records
from .storage import Conversation, ExperimentInitial, Observation, SceneRecord, Story

MAX_SPRITE_BYTES = 4 * 1024 * 1024


def asset_dir(config, owner):
    return config.data_dir / "sprites" / hashlib.sha256(owner.encode()).hexdigest()


def validate_png(data):
    if len(data) > MAX_SPRITE_BYTES:
        raise HTTPException(413, "图片请使用不超过 4 MB 的 PNG。")
    if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise HTTPException(422, "目前支持 PNG 图片，请先转换图片格式。")
    width, height = struct.unpack(">II", data[16:24])
    if not (0 < width <= 4096 and 0 < height <= 4096):
        raise HTTPException(422, "图片宽高须在 1 至 4096 像素之间。")


def presentation_router(db, config, owner):
    router = APIRouter(prefix="/api/role/presentation")

    def who(identity=Depends(owner)):
        if config.product != "roleplay":
            raise HTTPException(404)
        return identity

    from .video_assets import install_video_routes

    install_video_routes(router, config, who)

    from .video_playback import install_player_video_routes

    install_player_video_routes(router, db, config, who)

    @router.post("/sprites", status_code=201)
    async def upload(file: UploadFile = File(...), identity=Depends(who)):
        data = await file.read(MAX_SPRITE_BYTES + 1)
        validate_png(data)
        key = hashlib.sha256(data).hexdigest()
        folder = asset_dir(config, identity)
        folder.mkdir(parents=True, exist_ok=True)
        # Immutable content-addressed writes; concurrent equal uploads have equal bytes.
        path = folder / (key + ".png")
        if not path.exists():
            path.write_bytes(data)
        return {"id": key}

    @router.get("/sprites/{key}")
    def read(key: str, identity=Depends(who)):
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise HTTPException(404)
        path = asset_dir(config, identity) / (key + ".png")
        if not path.is_file():
            raise HTTPException(404, "立绘文件不存在，请在创作页面重新上传。")
        return {"data_url": "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()}

    @router.get("/stage/{actor_id}")
    def stage(actor_id: str, identity=Depends(who)):
        with db.session() as s:
            actor = s.scalar(
                select(Conversation).where(Conversation.id == actor_id, Conversation.owner_id == identity)
            )
            if not actor or not actor.current_state.get("player"):
                raise HTTPException(404, "玩家会话不存在。")
            return stage_view(s, actor)

    return router


def resolve_sprite(presentation, outfit=None, variant=None):
    """Bounded selection only: never infer an outfit from generated mood."""
    groups = presentation.get("outfits", [])
    selected = outfit or presentation.get("default_outfit") or (groups[0]["id"] if groups else None)
    group = next((o for o in groups if o["id"] == selected), None)
    choice = None
    if group:
        choice = next(
            (v for v in group["variants"] if v["id"] == (variant or group["default_variant"])), None
        )
        if choice is None:
            choice = next((v for v in group["variants"] if v["id"] == group["default_variant"]), None)
    return {
        "base_sprite": choice["sprite"] if choice else presentation.get("base_sprite"),
        "outfit": group["id"] if group else None,
        "variant": choice["id"] if choice else None,
        "variants": [{k: v.get(k) for k in ("id", "sprite", "expression", "pose")} for v in group["variants"]]
        if group
        else [],
    }


def freeze_speech_appearance(s, story, actor, payload):
    """Capture visual identity at commit, so historical reading never uses a future outfit."""
    if not (payload.get("narrative_node") or story.settings.get("narrative", {}).get("node")):
        return payload
    origin = s.get(ExperimentInitial, story.id)
    if not origin:
        return payload
    person = next(
        (p for p in origin.recipe["people"] if origin.receipt["actors"].get(p["alias"]) == actor.id), None
    )
    if not person:
        return payload
    node_id = payload.get("narrative_node") or story.settings.get("narrative", {}).get("node")
    graph = origin.recipe.get("narrative") or {}
    node = next((n for n in graph.get("nodes", []) if n["id"] == node_id), {})
    placement = next((p for p in node.get("stage") or [] if p["actor"] == person["alias"]), {})
    previous = (
        s.scalar(
            select(SceneRecord)
            .where(
                SceneRecord.story_id == story.id,
                SceneRecord.actor_id == actor.id,
                SceneRecord.kind == "speech",
                SceneRecord.redacted.is_(False),
                SceneRecord.payload["presentation_node"].as_string() == node_id,
            )
            .order_by(SceneRecord.seq.desc())
            .limit(1)
        )
        if node_id
        else None
    )
    prior = previous.payload.get("appearance", {}) if previous else {}
    block = next((b for b in node.get("opening") or [] if b["id"] == payload.get("opening_block")), {})
    outfit = block.get("outfit") or prior.get("outfit") or placement.get("outfit")
    variant = (
        block.get("variant")
        if block.get("outfit") or block.get("variant")
        else prior.get("variant", placement.get("variant"))
    )
    selected = resolve_sprite(person.get("presentation", {}), outfit, variant)
    appearance = {
        "sprite": selected["base_sprite"],
        "outfit": selected["outfit"],
        "variant": selected["variant"],
    }
    result = {**payload, "appearance": appearance, "presentation_node": node_id}
    if payload.get("performance"):
        parts = []
        for part in payload["performance"]:
            matches = [
                v
                for v in selected["variants"]
                if (v.get("expression") or v.get("pose"))
                and (not v.get("expression") or v["expression"] == part.get("expression"))
                and (not v.get("pose") or v["pose"] == part.get("pose"))
            ]
            matches.sort(key=lambda v: bool(v.get("expression")) + bool(v.get("pose")), reverse=True)
            visual = (
                {**appearance, "sprite": matches[0]["sprite"], "variant": matches[0]["id"]}
                if matches
                else appearance
            )
            parts.append({**part, "appearance": visual})
        result["performance"] = parts
    return result


def stage_view(s, actor):
    origin = s.get(ExperimentInitial, actor.story_id)
    assets = {}
    if origin:
        assets = {
            origin.receipt["actors"][p["alias"]]: p.get("presentation", {}) for p in origin.recipe["people"]
        }
    records = visible_records(s, actor.id, last=100)
    characters = [
        {**c, **resolve_sprite(assets.get(c["id"], {}))}
        for c in perceived_cast(s, actor)
        if c["id"] != actor.id
    ]
    if origin:
        authored_order = {
            origin.receipt["actors"][person["alias"]]: index
            for index, person in enumerate(origin.recipe["people"])
        }
        characters.sort(
            key=lambda character: (
                authored_order.get(character["id"], len(authored_order)),
                character["id"],
            )
        )
    # Presentation never widens physical perception or changes actor knowledge.
    story = s.get(Story, actor.story_id)
    graph = origin.recipe.get("narrative") if origin else None
    node_id = story.settings.get("narrative", {}).get("node") if story else None
    node = next((n for n in graph["nodes"] if n["id"] == node_id), None) if graph else None
    if node and node.get("stage") is not None:
        visible = {c["id"]: c for c in characters}
        characters = [
            {
                **visible[origin.receipt["actors"][p["actor"]]],
                "position": p["position"],
                "transform": p.get("transform"),
                "draw_order": p.get("draw_order") if p.get("draw_order") is not None else index,
                **resolve_sprite(
                    assets.get(origin.receipt["actors"][p["actor"]], {}), p.get("outfit"), p.get("variant")
                ),
            }
            for index, p in enumerate(node["stage"])
            if origin.receipt["actors"][p["actor"]] in visible
        ]
    if node and node.get("mode") == "backstage":
        characters = []
    # Only already perceived, unredacted author dialogue can apply its visual override.
    # Do not scan future opening blocks or infer changes from free-form model speech.
    if node:
        opening = {b["id"]: b for b in node.get("opening") or []}
        visible_characters = {c["id"]: c for c in characters}
        opening_rows = s.execute(
            select(SceneRecord, Observation)
            .join(Observation, Observation.event_id == SceneRecord.id)
            .where(
                SceneRecord.story_id == actor.story_id,
                Observation.actor_id == actor.id,
                SceneRecord.redacted.is_(False),
                SceneRecord.payload["narrative_node"].as_string() == node["id"],
                SceneRecord.kind == "speech",
            )
            .order_by(SceneRecord.seq)
            .limit(16)
        ).all()
        for record in (perceive(*row) for row in opening_rows):
            if (
                record.redacted
                or record.kind != "speech"
                or record.payload.get("narrative_node") != node["id"]
            ):
                continue
            block = opening.get(record.payload.get("opening_block"))
            character = visible_characters.get(record.actor_id)
            if block and character and (block.get("outfit") or block.get("variant")):
                character.update(
                    resolve_sprite(
                        assets.get(record.actor_id, {}),
                        block.get("outfit") or character.get("outfit"),
                        block.get("variant"),
                    )
                )
    last = next((r for r in reversed(records) if r.kind == "speech" and not r.redacted), None)
    return {
        "characters": characters,
        "objects": scene_objects(s, actor, story, node),
        "last_speech": {
            "speaker_id": last.actor_id,
            "text": last.text,
            "segments": last.payload.get("performance", []),
        }
        if last
        else None,
    }


def scene_objects(s, actor, story, node):
    """Project only currently visible physical items; never expose author-only flags."""
    if not node or node.get("mode") == "backstage" or not node.get("objects"):
        return []
    from .items.execution import view
    from .items.persistence import load
    from .storage import ItemWorldState

    visible = set()
    if any(o["kind"] == "item" for o in node["objects"]) and s.get(ItemWorldState, story.id):
        _, world = load(s, story.id, lock=False)
        perceived = {item["id"] for item in view(s, actor, world)}
        actor_state = next((state for state in world.actors if state.id == actor.id), None)
        # Scene transforms describe an item lying in this location. An item that
        # was picked up or put in a container remains visible in inventory, but
        # must no longer be drawn at its original position on the stage.
        visible = {
            item.id
            for item in world.items
            if item.id in perceived
            and item.placement.kind == "location"
            and actor_state is not None
            and item.placement.target == actor_state.location
        }
    aliases = story.settings.get("item_aliases", {})
    result = []
    for obj in node["objects"]:
        if obj["kind"] == "item" and aliases.get(obj["item"]) not in visible:
            continue
        projected = {
            key: value for key, value in obj.items() if key not in {"editor_locked", "editor_hidden", "item"}
        }
        if obj["kind"] == "item":
            projected["item_id"] = aliases[obj["item"]]
        result.append(projected)
    return result
