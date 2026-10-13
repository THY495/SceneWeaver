"""Windows launcher for an offline probe or a manifest-bound BYOK game."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import webview
from game_manifest import load_release
from managed_host import atomic_json
from windows_job import contain_current_host

from chatbot.native_paths import filesystem_path


def main(*, require_game=False, studio=False):
    parser = argparse.ArgumentParser()
    base = (
        Path(sys.executable).parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parents[2] / ".runtime/windows-p0/dist"
    )
    release = load_release(base)
    if studio and release:
        raise ValueError("开发工具目录不能包含游戏发行清单，请使用独立安装目录。")
    if require_game and release is None:
        raise FileNotFoundError("发行目录缺少 game.json，请保留完整游戏文件夹。")
    parser.add_argument("--backend", type=Path)
    parser.add_argument("--pgroot", type=Path, default=base / "pgsql")
    parser.add_argument("--data", type=Path)
    parser.add_argument("--smoke-report", type=Path)
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--game-package", type=Path)
    args = parser.parse_args()
    if studio and args.game_package:
        parser.error("请使用游戏自身的 Game.exe 启动作品")
    if studio:
        args.online = True
    if release:
        if args.game_package and args.game_package.resolve() != release["package"]:
            parser.error("不能用其他作品文件覆盖此游戏发行清单")
        args.game_package = release["package"]
    if args.game_package:
        args.game_package = args.game_package.resolve(strict=True)
        args.online = True
    args.backend = args.backend or base / (
        "GameBackend/GameBackend.exe"
        if args.game_package
        else "SceneweaverBackend/SceneweaverBackend.exe"
        if studio
        else "SceneweaverBackendP0/SceneweaverBackendP0.exe"
    )
    if args.data is None:
        local = Path(os.environ["LOCALAPPDATA"])
        if args.game_package:
            identity = (
                release["id"] if release else hashlib.sha256(str(args.game_package).encode()).hexdigest()
            )
            args.data = local / "AIVNGames" / identity
        else:
            args.data = local / ("Sceneweaver/Studio" if studio else "SceneweaverP0/desktop")
    args.data = args.data.resolve()
    studio_runtime = base / "game-runtime"
    if args.smoke_report:
        args.smoke_report = args.smoke_report.resolve()
    contain_current_host()
    args.data.mkdir(parents=True, exist_ok=True)
    control = args.data / ("control-" + uuid4().hex[:8])
    control.mkdir()
    if not args.backend.is_file() or not (args.pgroot / "bin/postgres.exe").is_file():
        raise FileNotFoundError("发行目录缺少后台或数据库文件，请保留完整发行文件夹。")
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "LOCALAPPDATA", "USERPROFILE"}
    }
    env["PATH"] = str(Path(os.environ["SYSTEMROOT"]) / "System32")
    with (control / "backend.log").open("wb") as log:
        child = subprocess.Popen(
            [
                str(filesystem_path(args.backend)),
                str(args.pgroot.resolve()),
                str(args.data / "world"),
                str(args.data / "aliases"),
                str(control),
                "--lifetime",
                "0",
                *(["--online"] if args.online else []),
                *(["--game-package", str(args.game_package)] if args.game_package else []),
                *(
                    ["--game-runtime", str(studio_runtime)]
                    if not args.game_package and studio_runtime.is_dir()
                    else []
                ),
            ],
            env=env,
            cwd=control,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            deadline = time.monotonic() + 40
            while not (control / "ready.json").exists():
                if child.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("独立后台启动失败，诊断位于 " + str(control / "backend.log"))
                time.sleep(0.1)
            ready = json.loads((control / "ready.json").read_text())
            window = webview.create_window(
                release["title"]
                if release
                else "游戏"
                if args.game_package
                else "Sceneweaver"
                if studio
                else "Sceneweaver · P0 离线试玩（假模型）",
                ready["launch_url"],
                width=1280,
                height=820,
                min_size=(900, 600),
                # Fit the Windows work area, including taskbar and DPI scaling.
                # A fixed 820px height can put the footer outside the desktop.
                maximized=not bool(args.smoke_report),
                hidden=bool(args.smoke_report),
            )
            if args.smoke_report:
                # Component smoke only: no claim of user-visible usability.
                def timeout():
                    time.sleep(25)
                    window.destroy()

                def verified(checks):
                    passed = isinstance(checks, dict) and all(
                        checks.get(key) == value
                        for key, value in {
                            "token_present": True,
                            "api_status": 200,
                            "expected_json": True,
                            "absent_status": 401,
                            "wrong_status": 401,
                            "replay_status": 403,
                        }.items()
                    )
                    args.smoke_report.parent.mkdir(parents=True, exist_ok=True)
                    atomic_json(
                        args.smoke_report,
                        {
                            "status": "window_loaded" if passed else "api_check_failed",
                            "renderer": "edgechromium",
                            "hidden": True,
                            "bootstrap_redirected": True,
                            "authenticated_api_ready": passed,
                            "authorization_checks": checks,
                        },
                    )
                    window.destroy()

                def loaded():
                    if window.get_current_url() == f"http://127.0.0.1:{ready['port']}/":
                        # A redirect alone missed a real Studio bootstrap-key mismatch.
                        # Exercise the appropriate frontend key without echoing the token.
                        token_key = "continuum-token" if args.game_package else "sceneweaver-token"
                        endpoint = "/api/game/library" if args.game_package else "/api/role/projects"
                        window.evaluate_js(
                            "(async () => { const token = sessionStorage.getItem("
                            + json.dumps(token_key)
                            + "); if (!token) return {token_present:false}; const endpoint = "
                            + json.dumps(endpoint)
                            + "; const response = await fetch(endpoint, {headers: {Authorization: 'Bearer ' + token}});"
                            + " const data = await response.json(); const shape = "
                            + (
                                "typeof data.title === 'string' && Array.isArray(data.saves)"
                                if args.game_package
                                else "Array.isArray(data) && data.every(p => typeof p.id === 'string')"
                            )
                            + "; const absent = await fetch(endpoint);"
                            + " const wrong = await fetch(endpoint, {headers:{Authorization:'Bearer invalid-smoke-token'}});"
                            + " const replay = await fetch("
                            + json.dumps(ready["launch_url"])
                            + "); return {token_present:true, api_status:response.status, expected_json:shape,"
                            + " absent_status:absent.status, wrong_status:wrong.status, replay_status:replay.status}; })()",
                            callback=verified,
                        )

                window.events.loaded += loaded
                threading.Thread(target=timeout, daemon=True).start()
            webview.start(gui="edgechromium", debug=False, private_mode=True)
        finally:
            (control / "stop").touch()
            try:
                child.wait(timeout=25)
            except subprocess.TimeoutExpired:
                # Exiting this launcher closes its owning Job, including nested
                # backend jobs. Do not kill unrelated processes by executable name.
                raise RuntimeError("后台停止超时，宿主将清理其进程树。") from None


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        if "--smoke-report" in sys.argv:
            raise
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, str(exc), "Sceneweaver 启动失败", 0x10)
        raise SystemExit(1)
