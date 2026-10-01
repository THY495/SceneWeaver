"""Assemble the authoring app with its separately packaged player runtime."""

import argparse
import shutil
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("dist", type=Path)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--studio-backend", type=Path)
    parser.add_argument("--game-backend", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    studio_backend = args.studio_backend or args.dist / "SceneweaverBackend"
    game_backend = args.game_backend or args.runtime / "GameBackend"
    for root in (args.dist, args.runtime, studio_backend, game_backend):
        source = root.resolve(strict=True)
        if output.is_relative_to(source) or source.is_relative_to(output):
            raise ValueError("Output must not overlap input directories")
    if output.exists():
        raise ValueError("Use a fresh Studio output directory")
    for file in (
        args.dist / "Sceneweaver/Sceneweaver.exe",
        studio_backend / "SceneweaverBackend.exe",
        args.runtime / "Game/Game.exe",
        game_backend / "GameBackend.exe",
        args.runtime / "pgsql/bin/postgres.exe",
    ):
        if not file.is_file():
            raise FileNotFoundError(file)
    shutil.copytree(args.dist / "Sceneweaver", output)
    shutil.copytree(studio_backend, output / "SceneweaverBackend")
    (output / "game-runtime").mkdir()
    for component, source in (
        ("Game", args.runtime / "Game"),
        ("GameBackend", game_backend),
        ("pgsql", args.runtime / "pgsql"),
    ):
        shutil.copytree(source, output / "game-runtime" / component)
    shutil.copytree(args.runtime / "pgsql", output / "pgsql")
    (output / "README.txt").write_text(
        "Sceneweaver\n\n双击 Sceneweaver.exe 打开开发工具。\n"
        "项目、预览与模型设置存储在当前 Windows 用户的本地应用数据目录。\n"
        "在项目中保存并构建 Windows 游戏；只向玩家分发构建出的完整游戏目录。\n"
        "玩家使用自己的模型密钥；不要向玩家分发此开发工具目录或个人数据。\n",
        encoding="utf-8",
    )
    print(str(output))


if __name__ == "__main__":
    main()
