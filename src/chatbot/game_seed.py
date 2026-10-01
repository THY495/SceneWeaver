"""Load a host-selected story package without installing author HTTP routes."""

import asyncio
import hashlib
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select, text

from .experiment_initials import digest
from .storage import ExperimentInitial, Story, uid
from .story_packages import MAX_PACKAGE, import_story_package


async def load_game_seed(db, config, package_path: Path) -> str:
    with Path(package_path).open("rb") as source:
        raw = source.read(MAX_PACKAGE + 1)
    if len(raw) > MAX_PACKAGE:
        raise HTTPException(413, "作品包不能超过32 MB。")
    package_hash = hashlib.sha256(raw).hexdigest()
    request_id = "release:" + package_hash
    owner = config.workspace_owner
    from .video_assets import video_dir
    from .video_sidecars import copy_videos

    videos = Path(package_path).parent / "videos"
    if not videos.resolve().is_relative_to(Path(package_path).parent.resolve()):
        raise HTTPException(422, "视频目录不能指向发行目录外。")
    lock = int.from_bytes(hashlib.sha256((owner + request_id).encode()).digest()[:8], "big", signed=True)
    # This lock covers local validation/import only, never model or network work.
    with db.session.begin() as s:
        s.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
        previous = s.scalar(
            select(ExperimentInitial).where(
                ExperimentInitial.owner_id == owner, ExperimentInitial.request_id == request_id
            )
        )
        if previous:
            story = s.get(Story, previous.story_id)
            if (
                not story
                or story.owner_id != owner
                or previous.request_hash != package_hash
                or previous.receipt.get("recipe_hash") != digest(previous.recipe)
            ):
                raise HTTPException(409, "已安装作品初始记录不一致，不能自动覆盖。")
            try:
                await asyncio.to_thread(copy_videos, previous.recipe, videos, video_dir(config, owner))
            except (ValueError, OSError) as exc:
                raise HTTPException(422, "视频素材无效：" + str(exc)) from None
            return previous.story_id
        imported = await import_story_package(raw, db, config, owner, video_source=videos)
        recipe = imported["recipe"]
        story_id = uid()
        s.add(
            Story(
                id=story_id,
                owner_id=owner,
                title=recipe["title"],
                world=recipe["world"],
                settings={"paused": True, "scene_enabled": False, "idle_enabled": False},
            )
        )
        s.flush()
        s.add(
            ExperimentInitial(
                story_id=story_id,
                owner_id=owner,
                request_id=request_id,
                request_hash=package_hash,
                recipe=recipe,
                receipt={"package_hash": package_hash, "recipe_hash": digest(recipe), "release_seed": True},
            )
        )
        return story_id
