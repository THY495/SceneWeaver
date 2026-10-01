"""Portable author recipes with bounded, content-addressed PNG assets."""

import asyncio
import base64
import binascii
import hashlib
import json

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import ValidationError

from .characters import validate_profile
from .presentation import MAX_SPRITE_BYTES, asset_dir, validate_png
from .story_blueprints import Recipe
from .story_format import DIRECTED, MEDIA, package_format
from .story_package_documents import TextDocument, document_references, export_documents, import_documents

MAX_PACKAGE = 32 * 1024 * 1024
MAX_ENCODED_PNG = 4 * ((MAX_SPRITE_BYTES + 2) // 3)


def references(recipe: Recipe) -> set[str]:
    keys = {p.presentation.base_sprite for p in recipe.people if p.presentation.base_sprite}
    keys.update(v.sprite for p in recipe.people for o in p.presentation.outfits for v in o.variants)
    keys.update(a.content_hash for a in recipe.image_assets)
    if recipe.narrative:
        keys.update(n.background for n in recipe.narrative.nodes if n.background)
        keys.update(o.sprite for n in recipe.narrative.nodes for o in n.objects)
    return keys


def validate_recipe(db, config, recipe, identity, portable=False):
    try:
        with db.session() as s:
            for person in recipe.people:
                validate_profile(
                    s,
                    identity,
                    person.profile.model_copy(update={"document_ids": []}) if portable else person.profile,
                    config,
                )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


async def import_story_package(raw: bytes, db, config, identity: str, *, video_source=None):
    """Shared bounded importer for Studio uploads and host-selected release files."""
    if len(raw) > MAX_PACKAGE:
        raise HTTPException(413, "故事包不能超过 32 MB。")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("format") not in (
            "sceneweaver.story.v1",
            "sceneweaver.story.v2",
            "sceneweaver.story.v3",
            DIRECTED,
            MEDIA,
        ):
            raise ValueError()
        if set(value) - {"format", "recipe", "assets", "documents"}:
            raise ValueError()
        recipe = Recipe.model_validate(value["recipe"])
    except (ValueError, KeyError, TypeError, ValidationError, RecursionError):
        raise HTTPException(422, "故事包格式或设定无效，请用创作工作室检查后重新导出。") from None
    from .video_assets import video_dir
    from .video_sidecars import checked_files, copy_videos

    if recipe.video_assets and video_source is None:
        raise HTTPException(422, "此作品含视频侧文件，请打开完整工程或使用完整 Windows 游戏文件夹。")
    try:
        if recipe.video_assets:
            checked_files(recipe.model_dump(), video_source)
    except (ValueError, OSError) as exc:
        raise HTTPException(422, "视频素材无效：" + str(exc)) from None
    documents = {}
    if value["format"] in {"sceneweaver.story.v3", DIRECTED, MEDIA}:
        try:
            entries = value.get("documents")
            if not isinstance(entries, dict) or set(entries) != document_references(recipe):
                raise ValueError()
            documents = {key: TextDocument.model_validate(item) for key, item in entries.items()}
            if sum(len(d.parts) for d in documents.values()) > 20000:
                raise ValueError()
            if any(
                sum(len(p.content.encode("utf-8")) + len(p.section.encode("utf-8")) + 10 for p in d.parts)
                > 15 * 1024 * 1024
                for d in documents.values()
            ):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(422, "故事包文字资料缺失、格式无效或超过容量限制。") from None
        validate_recipe(db, config, recipe, identity, portable=True)
    else:
        if value.get("documents") or document_references(recipe):
            raise HTTPException(422, "旧故事包未携带角色资料，请在原设备重新导出新版故事包。")
        validate_recipe(db, config, recipe, identity)
    keys = references(recipe)
    folder = asset_dir(config, identity)
    decoded = {}
    if value["format"] in {"sceneweaver.story.v2", "sceneweaver.story.v3", DIRECTED, MEDIA}:
        assets = value.get("assets")
        if not isinstance(assets, dict) or set(assets) != keys:
            raise HTTPException(422, "故事包素材与设定引用不一致，请重新导出完整故事包。")
        for key, encoded in assets.items():
            try:
                if not isinstance(encoded, str) or len(encoded) > MAX_ENCODED_PNG:
                    raise ValueError()
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise HTTPException(422, "故事包图片编码无效。") from None
            validate_png(data)
            if hashlib.sha256(data).hexdigest() != key:
                raise HTTPException(422, "故事包图片校验失败。")
            decoded[key] = data
    elif any(not (folder / (key + ".png")).is_file() for key in keys):
        raise HTTPException(422, "旧故事包没有携带图片，请在原设备重新导出新版故事包。")
    # Verify every entry before writing anything. Only referenced hashes become filenames.
    if recipe.video_assets:
        await asyncio.to_thread(copy_videos, recipe.model_dump(), video_source, video_dir(config, identity))
    if decoded:
        folder.mkdir(parents=True, exist_ok=True)
        for key, data in decoded.items():
            (folder / (key + ".png")).write_bytes(data)
    if documents:

        def restore_documents():
            with db.session.begin() as s:
                import_documents(s, config, identity, recipe, documents)

        await asyncio.to_thread(restore_documents)
    return {
        "recipe": recipe.model_dump(),
        "asset_count": len(keys),
        "document_count": len(documents),
        "document_index": "lexical" if documents else None,
    }


def package_router(db, config, owner):
    router = APIRouter(prefix="/api/role/packages")

    def who(identity=Depends(owner)):
        if config.product != "roleplay":
            raise HTTPException(404)
        return identity

    @router.post("/export")
    def export(recipe: Recipe, identity=Depends(who)):
        if recipe.video_assets:
            raise HTTPException(422, "视频不能装入单个 JSON 文件，请保存完整工程并构建 Windows 游戏。")
        validate_recipe(db, config, recipe, identity)
        recipe = recipe.model_copy(deep=True)
        try:
            with db.session() as s:
                documents = export_documents(s, identity, recipe)
        except ValidationError:
            raise HTTPException(422, "资料片段不符合可迁移格式，请重新建立索引。") from None
        if sum(len(d["parts"]) for d in documents.values()) > 20000 or any(
            sum(len(p["content"].encode()) + len(p["section"].encode()) + 10 for p in d["parts"])
            > 15 * 1024 * 1024
            for d in documents.values()
        ):
            raise HTTPException(413, "资料快照超过容量限制，请精简后导出。")
        assets = {}
        size = 0
        for key in sorted(references(recipe)):
            path = asset_dir(config, identity) / (key + ".png")
            if not path.is_file():
                raise HTTPException(422, "故事引用的图片缺失，请补传图片或移除引用后重新导出。")
            data = path.read_bytes()
            validate_png(data)
            if hashlib.sha256(data).hexdigest() != key:
                raise HTTPException(422, "图片校验失败，请重新上传。")
            encoded = base64.b64encode(data).decode()
            size += len(encoded)
            if size > MAX_PACKAGE:
                raise HTTPException(413, "故事包超过 32 MB，请缩小图片后导出。")
            assets[key] = encoded
        result = {
            "format": package_format(recipe, documents=bool(documents)),
            "recipe": recipe.model_dump(),
            "assets": assets,
        }
        if documents or result["format"] in {DIRECTED, MEDIA}:
            result.update(documents=documents)
        if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_PACKAGE:
            raise HTTPException(413, "故事包超过 32 MB，请缩小图片后导出。")
        return result

    @router.post("/import")
    async def import_package(file: UploadFile = File(...), identity=Depends(who)):
        raw = await file.read(MAX_PACKAGE + 1)
        return await import_story_package(raw, db, config, identity)

    return router
