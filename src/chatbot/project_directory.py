"""Portable authoring directories; no database or provider connection required.

Immutable content snapshots are committed by replacing the manifest last.
The lock protects cooperating writers; external edits are detected on open.
"""

import argparse
import base64
import hashlib
import json
import os
import re
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException

from .presentation import validate_png
from .project_drafts import draft_recipe
from .story_format import DIRECTED, MEDIA, has_direction, has_media
from .story_package_documents import TextDocument, document_references
from .story_packages import MAX_PACKAGE, references
from .version import PUBLIC_VERSION

FORMAT = "sceneweaver.project.v1"
DIRECTED_FORMAT = "sceneweaver.project.v2"
MEDIA_FORMAT = "sceneweaver.project.v3"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


def bounded_read(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_PACKAGE + 1)
    if len(raw) > MAX_PACKAGE:
        raise ValueError("工程内容超过 32 MB 限制")
    return raw


def local_path(root, relative):
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("工程路径不能指向目录外部")
    return path


def validate_package(value):
    if (
        not isinstance(value, dict)
        or value.get("format") not in {"sceneweaver.story.v2", "sceneweaver.story.v3", DIRECTED, MEDIA}
        or set(value) - {"format", "recipe", "assets", "documents"}
    ):
        raise ValueError("请使用携带资源的 v2/v3/v4/v5 故事包")
    recipe = draft_recipe(value["recipe"])
    assets = value.get("assets", {})
    if not isinstance(assets, dict) or set(assets) != references(recipe):
        raise ValueError("图片与故事引用不一致")
    for key, data in assets.items():
        raw = base64.b64decode(data, validate=True)
        try:
            validate_png(raw)
        except HTTPException as exc:
            raise ValueError(str(exc.detail)) from None
        if hashlib.sha256(raw).hexdigest() != key:
            raise ValueError("图片校验失败")
    documents = value.get("documents", {})
    if not isinstance(documents, dict) or set(documents) != document_references(recipe):
        raise ValueError("文字资料与故事引用不一致")
    if documents and value["format"] not in {"sceneweaver.story.v3", DIRECTED, MEDIA}:
        raise ValueError("文字资料需要 v3 故事包")
    parsed = {key: TextDocument.model_validate(doc).model_dump() for key, doc in documents.items()}
    if sum(len(doc["parts"]) for doc in parsed.values()) > 20000:
        raise ValueError("文字资料片段过多")
    # Early authoring previews used v3 for direction. Upgrade only the returned
    # value; opening never modifies source files. Save retains the old snapshot.
    result = dict(
        format=MEDIA if has_media(recipe) else DIRECTED if has_direction(recipe) else value["format"],
        recipe=recipe.model_dump(),
        assets=assets,
        documents=parsed,
    )
    if len(encoded(result)) > MAX_PACKAGE:
        raise ValueError("工程内容超过 32 MB 限制")
    return result


def read_manifest(root):
    value = json.loads(bounded_read(local_path(root, "sceneweaver.project.json")))
    if not isinstance(value, dict) or value.get("format") not in {FORMAT, DIRECTED_FORMAT, MEDIA_FORMAT}:
        raise ValueError("不支持的工程格式版本")
    UUID(value["id"])
    if type(value.get("revision")) is not int or value["revision"] < 1:
        raise ValueError("无效工程版本")
    if not re.fullmatch(r"[a-f0-9]{64}", value.get("content", "")):
        raise ValueError("无效内容引用")
    return value


def open_project(directory):
    root = Path(directory)
    manifest = read_manifest(root)
    raw = bounded_read(local_path(root, f"content/{manifest['content']}.json"))
    if hashlib.sha256(raw).hexdigest() != manifest["content"]:
        raise ValueError("内容已被外部修改或损坏")
    payload = json.loads(raw)
    recipe = draft_recipe(payload["recipe"])
    from .video_sidecars import checked_files

    checked_files(payload["recipe"], local_path(root, "videos"))
    assets = {}
    for key in sorted(references(recipe)):
        data = bounded_read(local_path(root, f"assets/{key}.png"))
        assets[key] = base64.b64encode(data).decode("ascii")
        if sum(len(item) for item in assets.values()) > MAX_PACKAGE:
            raise ValueError("图片总量超过限制")
    return manifest, validate_package({**payload, "assets": assets})


def save_project(directory, package, *, expected_revision, project_id=None, video_source=None):
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("无效预期版本")
    if project_id:
        UUID(project_id)
    package = validate_package(package)
    root = Path(directory)
    from .video_sidecars import checked_files, copy_videos

    source = video_source if video_source is not None else local_path(root, "videos")
    checked_files(package["recipe"], source)
    # Creation is explicit and never adopts an existing unrelated directory.
    if expected_revision == 0:
        root.mkdir(parents=False, exist_ok=False)
    elif not root.is_dir():
        raise ValueError("工程目录不存在")
    lock = local_path(root, ".sceneweaver-write.lock")
    try:
        handle = lock.open("x")
    except FileExistsError:
        raise ValueError("工程正在保存；若进程意外退出，请确认没有写入者后再移除锁文件") from None
    try:
        with handle:
            handle.write(str(os.getpid()))
        prior = read_manifest(root) if expected_revision else None
        if prior and prior["revision"] != expected_revision:
            raise ValueError("工程版本冲突，请重新打开后合并修改")
        if prior and project_id and prior["id"] != project_id:
            raise ValueError("工程身份不匹配")
        identity = prior["id"] if prior else str(UUID(project_id)) if project_id else str(uuid4())
        copy_videos(package["recipe"], source, local_path(root, "videos"))
        for name in ("content", "assets", ".sceneweaver", "builds"):
            local_path(root, name).mkdir(exist_ok=True)
        for key, data in package["assets"].items():
            destination = local_path(root, f"assets/{key}.png")
            raw = base64.b64decode(data)
            if destination.exists():
                if bounded_read(destination) != raw:
                    raise ValueError("已有图片损坏，拒绝覆盖")
            else:
                with destination.open("xb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
        payload = {key: value for key, value in package.items() if key != "assets"}
        raw = encoded(payload)
        digest = hashlib.sha256(raw).hexdigest()
        snapshot = local_path(root, f"content/{digest}.json")
        if snapshot.exists():
            if bounded_read(snapshot) != raw:
                raise ValueError("已有内容快照损坏")
        else:
            with snapshot.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        manifest = dict(
            format=MEDIA_FORMAT
            if package["format"] == MEDIA
            else DIRECTED_FORMAT
            if package["format"] == DIRECTED
            else FORMAT,
            id=identity,
            revision=expected_revision + 1,
            title=package["recipe"]["title"],
            content=digest,
            runtime=PUBLIC_VERSION,
        )
        temporary = local_path(root, ".sceneweaver/manifest-" + str(uuid4()) + ".tmp")
        with temporary.open("xb") as stream:
            stream.write(encoded(manifest))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, local_path(root, "sceneweaver.project.json"))
        return manifest
    finally:
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description="Sceneweaver 磁盘工程工具（不连接数据库）")
    parser.add_argument("command", choices=["import", "check", "export", "save"])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--expected-revision", type=int)
    args = parser.parse_args()
    try:
        if args.command in {"import", "save"}:
            if not args.package or (args.command == "save" and args.expected_revision is None):
                parser.error("需要 --package；save 还需要 --expected-revision")
            revision = 0 if args.command == "import" else args.expected_revision
            if args.command == "save" and revision < 1:
                parser.error("save 的版本必须大于零")
            result = save_project(
                args.directory,
                json.loads(bounded_read(args.package)),
                expected_revision=revision,
                video_source=local_path(args.package.parent, "videos"),
            )
        else:
            result, package = open_project(args.directory)
            if args.command == "export":
                if not args.package:
                    parser.error("需要 --package 指定输出文件")
                from .video_sidecars import copy_videos

                if args.package.exists():
                    raise ValueError("导出文件已经存在，不能覆盖")
                copy_videos(
                    package["recipe"],
                    local_path(args.directory, "videos"),
                    local_path(args.package.parent, "videos"),
                )
                with args.package.open("xb") as stream:
                    stream.write(encoded(package))
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, OSError, KeyError, TypeError, RecursionError) as exc:
        parser.exit(1, f"工程操作失败：{exc}\n")


if __name__ == "__main__":
    main()
