"""Managed disk projects with disposable database snapshots for existing previews."""

import base64
import hashlib
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from .characters import validate_profile
from .presentation import asset_dir
from .project_directory import open_project, save_project, validate_package
from .project_drafts import DraftBlueprintSave, draft_recipe
from .storage import StoryBlueprint, now
from .story_package_documents import TextDocument, export_documents, import_documents
from .story_packages import references
from .video_assets import video_dir


class BuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    request_id: UUID | None = None


def project_root(config, identity):
    # Keep managed paths usable under Windows' legacy path length limit.
    return config.data_dir / "projects" / hashlib.sha256(identity.encode()).hexdigest()[:24]


def project_router(db, config, owner):
    router = APIRouter(prefix="/api/role/projects")

    def who(identity=Depends(owner)):
        if config.product != "roleplay":
            raise HTTPException(404)
        return identity

    def folder(identity, pid):
        try:
            key = str(UUID(pid))
        except ValueError:
            raise HTTPException(422, "工程标识无效") from None
        return project_root(config, identity) / key

    def lock(s, pid):
        key = int.from_bytes(hashlib.sha256(f"blueprint:{pid}".encode()).digest()[:8], "big", signed=True)
        s.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    def snapshot(s, identity, recipe):
        recipe = recipe.model_copy(deep=True)
        for person in recipe.people:
            validate_profile(s, identity, person.profile, config)
        documents = export_documents(s, identity, recipe)
        assets = {}
        for key in references(recipe):
            path = asset_dir(config, identity) / (key + ".png")
            assets[key] = base64.b64encode(path.read_bytes()).decode("ascii")
        return validate_package(
            dict(
                format="sceneweaver.story.v3", recipe=recipe.model_dump(), assets=assets, documents=documents
            )
        )

    def materialize(s, identity, manifest, package):
        from .video_sidecars import copy_videos

        copy_videos(
            package["recipe"], folder(identity, manifest["id"]) / "videos", video_dir(config, identity)
        )
        row = s.get(StoryBlueprint, manifest["id"])
        if row and row.owner_id != identity:
            raise HTTPException(409, "工程标识已被占用")
        # Assets are reconstructible from the project even after cache removal.
        destination = asset_dir(config, identity)
        destination.mkdir(parents=True, exist_ok=True)
        for key, data in package["assets"].items():
            (destination / (key + ".png")).write_bytes(base64.b64decode(data))
        if not row or row.version != manifest["revision"]:
            recipe = draft_recipe(package["recipe"])
            if package["documents"]:
                import_documents(
                    s,
                    config,
                    identity,
                    recipe,
                    {key: TextDocument.model_validate(doc) for key, doc in package["documents"].items()},
                    preserve_name=True,
                )
            if not row:
                row = StoryBlueprint(id=manifest["id"], owner_id=identity)
                s.add(row)
            row.recipe = recipe.model_dump()
            row.version = manifest["revision"]
            row.updated_at = now()
        return dict(id=row.id, version=row.version, recipe=row.recipe, storage="directory")

    @router.get("")
    def listing(identity=Depends(who)):
        root = project_root(config, identity)
        result = []
        if not root.exists():
            return result
        for path in sorted(root.iterdir()):
            if not path.is_dir():
                continue
            try:
                UUID(path.name)
                manifest, package = open_project(path)
                result.append(
                    dict(
                        id=manifest["id"],
                        version=manifest["revision"],
                        recipe=package["recipe"],
                        storage="directory",
                        path=str(path),
                    )
                )
            except (ValueError, OSError, KeyError, TypeError):
                # Broken projects remain visible instead of silently disappearing.
                result.append(dict(id=path.name, error="工程缺失或损坏，请检查目录", path=str(path)))
        return result

    @router.get("/{pid}")
    def get(pid: str, identity=Depends(who)):
        try:
            with db.session.begin() as s:
                lock(s, pid)
                manifest, package = open_project(folder(identity, pid))
                if manifest["id"] != pid:
                    raise ValueError("工程目录与身份不一致")
                return {**materialize(s, identity, manifest, package), "path": str(folder(identity, pid))}
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise HTTPException(422, "无法打开磁盘工程：" + str(exc)) from None

    @router.put("/{pid}")
    def put(pid: str, body: DraftBlueprintSave, identity=Depends(who)):
        path = folder(identity, pid)
        try:
            with db.session.begin() as s:
                lock(s, pid)
                package = snapshot(s, identity, body.recipe)
                if path.exists():
                    manifest, existing = open_project(path)
                    if manifest["id"] != pid:
                        raise ValueError("工程身份不一致")
                    if manifest["revision"] == body.expected_version + 1 and existing == package:
                        return {**materialize(s, identity, manifest, existing), "path": str(path)}
                    if manifest["revision"] != body.expected_version:
                        raise HTTPException(409, "磁盘工程已更新，请重开工程；当前草稿仍保留")
                elif body.expected_version:
                    raise HTTPException(409, "工程目录已缺失，请恢复目录后保存")
                else:
                    if s.get(StoryBlueprint, pid):
                        raise HTTPException(409, "旧记录请通过导入副本迁移，不能原地覆盖")
                    path.parent.mkdir(parents=True, exist_ok=True)
                manifest = save_project(
                    path,
                    package,
                    expected_revision=body.expected_version,
                    project_id=pid,
                    video_source=video_dir(config, identity),
                )
                return {**materialize(s, identity, manifest, package), "path": str(path)}
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise HTTPException(422, "磁盘工程未完成保存：" + str(exc)) from None

    @router.post("/{pid}/builds", status_code=201)
    def build(pid: str, body: BuildRequest, identity=Depends(who)):
        from .game_build import build_project

        if config.windows_game_runtime_dir is None:
            raise HTTPException(503, "此开发工具尚未安装 Windows 游戏运行库，暂时无法构建")
        try:
            with db.session.begin() as s:
                lock(s, pid)
                path = folder(identity, pid)
                manifest, _ = open_project(path)
                if manifest["id"] != pid:
                    raise ValueError("工程身份不一致")
                if manifest["revision"] != body.expected_version:
                    raise HTTPException(409, "工程已变化，请保存并重新构建")
                return build_project(
                    path, config.windows_game_runtime_dir, body.expected_version, body.request_id
                )
        except OSError:
            logging.getLogger(__name__).exception("Windows project build failed for %s", pid)
            raise HTTPException(
                422, "无法写入游戏文件。工程已保留，请检查磁盘空间、目录权限及后台构建日志。"
            ) from None
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(422, "游戏构建失败：" + str(exc)) from None

    @router.get("/{pid}/builds")
    def builds(pid: str, identity=Depends(who)):
        from .game_build import list_builds

        try:
            path = folder(identity, pid)
            manifest, _ = open_project(path)
            if manifest["id"] != pid:
                raise ValueError("工程身份不一致")
            return list_builds(path)
        except (ValueError, OSError, KeyError, TypeError):
            raise HTTPException(422, "无法读取本工程构建记录") from None

    return router
