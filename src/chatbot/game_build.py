"""Build a saved project using a host-selected, credential-free Windows runtime."""

import base64
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from .native_paths import filesystem_path
from .project_directory import encoded, local_path, open_project
from .release_manifest import load_release, validate_manifest
from .story_blueprints import Recipe

RUNTIME_COMPONENTS = ("Game", "GameBackend", "pgsql")
GENERATED_GAME_ROOT = frozenset({"GameBackend", "pgsql", "videos", "story.json", "game.json", "README.txt"})
CAPABILITIES_FILE = "sceneweaver-runtime-capabilities.json"
LEGACY_MAX_PNG_BYTES = 2 * 1024 * 1024
LEGACY_MAX_ENCODED_PNG = 2_800_000


def runtime_image_limits(runtime: Path) -> tuple[int, int]:
    """Old runtimes predate capability metadata and retain the original 2 MiB bound."""
    path = runtime / "GameBackend" / CAPABILITIES_FILE
    if not path.exists():
        return LEGACY_MAX_PNG_BYTES, LEGACY_MAX_ENCODED_PNG
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("format") != "sceneweaver.game-runtime-capabilities.v1"
            or type(value.get("max_png_bytes")) is not int
            or type(value.get("max_encoded_png_chars")) is not int
            or not 0 < value["max_png_bytes"] <= 32 * 1024 * 1024
            or not 0 < value["max_encoded_png_chars"] <= 44 * 1024 * 1024
        ):
            raise ValueError()
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise ValueError("Windows 游戏运行库的图片能力清单无效，请重新安装运行库") from None
    return value["max_png_bytes"], value["max_encoded_png_chars"]


def validate_runtime_images(runtime: Path, package: dict) -> None:
    max_bytes, max_encoded = runtime_image_limits(runtime)
    asset_names = {
        item.get("content_hash"): item.get("name")
        for item in package.get("recipe", {}).get("image_assets", [])
        if isinstance(item, dict)
    }
    for image_hash, image_base64 in package.get("assets", {}).items():
        image = base64.b64decode(image_base64)
        if len(image_base64) > max_encoded or len(image) > max_bytes:
            name = asset_names.get(image_hash) or image_hash[:12]
            raise ValueError(
                f"图片「{name}」为 {len(image) / (1024 * 1024):.2f} MiB；"
                f"当前 Windows 游戏运行库最多支持 {max_bytes / (1024 * 1024):g} MiB PNG。"
                "请换用更小的图片，或更新游戏运行库后重试"
            )


def component_sha256(root: Path, *, exclude_top: frozenset[str] = frozenset()) -> str:
    """Bind every runtime file path and byte, including nested dependencies."""
    native = filesystem_path(root)
    if native.is_symlink() or not native.is_dir():
        raise ValueError("Windows 运行库组件缺失")
    files = []
    excluded = {name.casefold() for name in exclude_top}
    for path in native.rglob("*"):
        if path.relative_to(native).parts[0].casefold() in excluded:
            continue
        if path.is_symlink():
            raise ValueError("Windows 运行库不能包含符号链接")
        if path.is_file():
            files.append(path)
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda file: file.relative_to(native).as_posix()):
        name = path.relative_to(native).as_posix()
        with path.open("rb") as source:
            content = hashlib.file_digest(source, "sha256").digest()
        digest.update(name.encode("utf-8") + b"\0" + content)
    return digest.hexdigest()


def runtime_sha256(components: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name in RUNTIME_COMPONENTS:
        digest.update(name.encode("ascii") + b"\0" + bytes.fromhex(components[name]))
    return digest.hexdigest()


def build_project(project: Path, runtime: Path, expected_revision: int, request_id=None):
    manifest, package = open_project(project)
    if manifest["revision"] != expected_revision:
        raise ValueError("工程版本已变化，请保存并重新构建")
    recipe = Recipe.model_validate(package["recipe"])  # Draft-only graphs cannot be published.
    if recipe.narrative:
        from .backstage_release import release_plan

        release_plan(
            recipe.narrative.model_dump(), [p.model_dump() for p in recipe.people], recipe.player.model_dump()
        )
    raw = encoded(package)
    release = dict(
        format="ai-novel-game.v1",
        id=manifest["id"],
        title=package["recipe"]["title"],
        package_sha256=hashlib.sha256(raw).hexdigest(),
    )
    validate_manifest(release)
    # Runtime roots are configured by the host, never supplied by an HTTP client.
    for entry in ("Game/Game.exe", "GameBackend/GameBackend.exe", "pgsql/bin/postgres.exe"):
        if not (runtime / entry).is_file():
            raise ValueError("Windows 运行库不完整，请安装开发工具的游戏运行库")
    validate_runtime_images(runtime, package)
    if any((runtime / "Game" / name).exists() for name in GENERATED_GAME_ROOT):
        raise ValueError("游戏桌面运行库占用了发行包保留路径")
    component_hashes = {name: component_sha256(runtime / name) for name in RUNTIME_COMPONENTS}
    runtime_hash = runtime_sha256(component_hashes)
    builds = local_path(project, "builds")
    builds.mkdir(exist_ok=True)
    build_id = UUID(str(request_id)).hex if request_id else uuid4().hex
    staging = local_path(project, "builds/.building-" + build_id)
    destination = local_path(project, "builds/windows-" + build_id)
    records = local_path(project, "builds/.records")
    records.mkdir(exist_ok=True)
    receipt = records / (build_id + ".json")
    result = dict(
        id=build_id,
        project_id=manifest["id"],
        revision=manifest["revision"],
        title=release["title"],
        path=str(destination),
        entry="Game.exe",
        package_sha256=release["package_sha256"],
        runtime_sha256=runtime_hash,
        created_at=datetime.now(UTC).isoformat(),
    )
    if receipt.exists():
        previous = json.loads(receipt.read_text(encoding="utf-8"))
        if any(previous.get(key) != result[key] for key in ("project_id", "revision", "package_sha256")):
            raise ValueError("同一构建请求不能更改工程版本或内容")
        if previous.get("runtime_sha256") != runtime_hash:
            raise ValueError("游戏运行库已变化或旧构建缺少运行库校验，请创建新的构建请求")
        result = previous
        if destination.exists():
            installed = load_release(filesystem_path(destination))
            if not installed or installed["package_sha256"] != result["package_sha256"]:
                raise ValueError("已有构建损坏，请创建新的构建")
            for name in RUNTIME_COMPONENTS:
                copied = destination if name == "Game" else destination / name
                digest = (
                    component_sha256(copied, exclude_top=GENERATED_GAME_ROOT)
                    if name == "Game"
                    else component_sha256(copied)
                )
                if digest != component_hashes[name]:
                    raise ValueError("已有构建的游戏运行库已变化，请创建新的构建")
            return result
        if staging.exists():
            raise ValueError("此前构建未完成，现场已保留；请发起新构建")
    else:
        with receipt.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False)
    for source in (runtime / name for name in RUNTIME_COMPONENTS):
        if staging.resolve().is_relative_to(source.resolve()) or source.resolve().is_relative_to(
            staging.resolve()
        ):
            raise ValueError("运行库与构建目录不能重叠")
    # Completion is an atomic rename; failed staging remains available for diagnosis.
    staging = filesystem_path(staging)
    shutil.copytree(filesystem_path(runtime / "Game"), staging)
    if component_sha256(staging) != component_hashes["Game"]:
        raise ValueError("游戏桌面运行库复制后校验不一致")
    shutil.copytree(filesystem_path(runtime / "GameBackend"), staging / "GameBackend")
    if component_sha256(staging / "GameBackend") != component_hashes["GameBackend"]:
        raise ValueError("游戏后台运行库复制后校验不一致")
    shutil.copytree(filesystem_path(runtime / "pgsql"), staging / "pgsql")
    if component_sha256(staging / "pgsql") != component_hashes["pgsql"]:
        raise ValueError("数据库运行库复制后校验不一致")
    (staging / "story.json").write_bytes(raw)
    from .video_sidecars import copy_videos

    copy_videos(package["recipe"], local_path(project, "videos"), staging / "videos")
    (staging / "game.json").write_text(json.dumps(release, ensure_ascii=False), encoding="utf-8")
    load_release(staging)
    (staging / "README.txt").write_text(
        release["title"] + "\n\n双击 Game.exe 开始，保留完整游戏文件夹。\n"
        "首次游玩需配置自己的 LLM API Key，语音另需 TTS 配置。\n"
        "存档与加密凭据保存在本机用户目录。需要 Windows WebView2 Runtime 和 .NET。\n",
        encoding="utf-8",
    )
    staging.rename(filesystem_path(destination))
    return result


def list_builds(project: Path):
    records = local_path(project, "builds/.records")
    if not records.exists():
        return []
    result = []
    for file in records.glob("*.json"):
        try:
            identity = UUID(file.stem).hex
            row = json.loads(file.read_text(encoding="utf-8"))
            destination = local_path(project, "builds/windows-" + identity)
            status = "incomplete"
            if destination.exists():
                try:
                    release = load_release(filesystem_path(destination))
                    status = (
                        "completed"
                        if release and release["package_sha256"] == row["package_sha256"]
                        else "damaged"
                    )
                except (ValueError, OSError, KeyError, TypeError):
                    status = "damaged"
            result.append(
                {key: row[key] for key in ("project_id", "revision", "title", "created_at")}
                | {
                    "id": identity,
                    "path": str(destination),
                    "status": status,
                    "runtime_sha256": row.get("runtime_sha256"),
                }
            )
        except (ValueError, OSError, TypeError, KeyError):
            continue
    return sorted(result, key=lambda row: row.get("created_at", ""), reverse=True)
