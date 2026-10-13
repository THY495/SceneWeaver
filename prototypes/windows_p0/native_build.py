"""Windows build entry: pinned combined dependencies, isolated env, and receipts."""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "prototypes/windows_p0"
LOCK = HERE / "native-build.lock"
INPUTS = HERE / "native-lock-inputs.json"
TARGETS = {
    "Game": "game_launcher.py",
    "Sceneweaver": "studio_launcher.py",
    "GameBackend": "backend_host.py",
    "SceneweaverBackend": "backend_host.py",
    "SceneweaverDesktopP0": "desktop_host.py",
    "SceneweaverBackendP0": "backend_host.py",
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text_sha(path):
    return hashlib.sha256(path.read_text("utf-8").encode("utf-8")).hexdigest()


def lock_inputs():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    return {
        "format": "sceneweaver.native-lock.v1",
        "python": "3.13.9",
        "platform": "win32-amd64",
        "files": {
            name: text_sha(ROOT / name)
            for name in ("uv.lock", "prototypes/windows_p0/requirements-native-tools.in")
        },
        "application_constraints": {
            "dependencies": project["project"]["dependencies"],
            "requires-python": project["project"]["requires-python"],
            "build-system": project["build-system"],
        },
    }


def validate_lock():
    metadata = json.loads(INPUTS.read_text("utf-8"))
    if any(metadata.get(key) != value for key, value in lock_inputs().items()) or metadata.get(
        "lock_sha256"
    ) != text_sha(LOCK):
        raise ValueError("Native lock inputs changed; maintainer must regenerate and review the lock")
    return metadata


def checked_output(value):
    output = Path(value).resolve()
    runtime = (ROOT / ".runtime").resolve()
    if output == runtime or not output.is_relative_to(runtime):
        raise ValueError("Build output must be a subdirectory of this checkout's .runtime")
    return output


def build_environment(output):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CHAT_", "UV_"))
        and key not in {"VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "NODE_OPTIONS"}
    }
    env.update(
        PYTHONHASHSEED="0",
        SOURCE_DATE_EPOCH="1791670000",
        UV_CACHE_DIR=str(output / "uv-cache"),
        PYINSTALLER_CONFIG_DIR=str(output / "pyinstaller-cache"),
    )
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=("lock", "prepare", "all", *TARGETS))
    parser.add_argument("--output", default=str(ROOT / ".runtime/windows-p0"))
    args = parser.parse_args()
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise ValueError("Native builds require Windows x64")
    if sys.version_info[:3] != (3, 13, 9):
        raise ValueError("Use Python 3.13.9; the combined lock is validated for that interpreter")
    output = checked_output(args.output)
    output.mkdir(parents=True, exist_ok=True)
    env = build_environment(output)
    receipt_dir = output / "receipts" / (args.target + "-" + uuid4().hex)
    receipt_dir.mkdir(parents=True)
    result = {"target": args.target, "steps": [], "completed": False, "scope": "Windows build, not playtest"}
    python = output / "env/Scripts/python.exe"
    uv = shutil.which("uv")
    if not uv:
        raise ValueError("Install uv before building")

    def run(label, command, *, cwd=ROOT):
        log = receipt_dir / (label + ".log")
        started = time.monotonic()
        try:
            with log.open("w", encoding="utf-8") as stream:
                done = subprocess.run(
                    [str(part) for part in command],
                    cwd=cwd,
                    env=env,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    timeout=900,
                )
            code = done.returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
        result["steps"].append(
            {
                "name": label,
                "exit_code": code,
                "seconds": round(time.monotonic() - started, 3),
                "log": str(log),
            }
        )
        print(json.dumps(result["steps"][-1]), flush=True)
        if code != 0:
            raise RuntimeError(f"{label} failed; inspect {log}")

    try:
        run("uv-version", [uv, "--version"])
        if args.target != "lock":
            result["lock_inputs"] = validate_lock()
        if not python.is_file():
            if (output / "env").exists():
                raise ValueError("Incomplete build environment preserved; select a new --output")
            run("venv", [uv, "--no-config", "venv", output / "env", "--python", sys.executable])
        if args.target == "lock":
            run(
                "app-export",
                [
                    uv,
                    "--no-config",
                    "export",
                    "--locked",
                    "--no-dev",
                    "--no-emit-project",
                    "--output-file",
                    output / "app-lock.txt",
                ],
            )
            run(
                "combined-lock",
                [
                    uv,
                    "--no-config",
                    "pip",
                    "compile",
                    output / "app-lock.txt",
                    HERE / "requirements-native-tools.in",
                    "--python",
                    python,
                    "--generate-hashes",
                    "--no-header",
                    "--no-annotate",
                    "-o",
                    LOCK,
                ],
            )
            INPUTS.write_text(
                json.dumps({**lock_inputs(), "lock_sha256": text_sha(LOCK)}, indent=2) + "\n", "utf-8"
            )
            result["lock_inputs"] = json.loads(INPUTS.read_text("utf-8"))
        else:
            run("application-lock-check", [uv, "--no-config", "lock", "--check"])
            result["source_files"] = {
                str(path.relative_to(ROOT)).replace("\\", "/"): sha(path)
                for path in sorted((ROOT / "src").rglob("*"))
                if path.is_file() and "__pycache__" not in path.parts
            }
            run(
                "dependency-sync",
                [uv, "--no-config", "pip", "sync", "--python", python, "--require-hashes", LOCK],
            )
            # Assemble tools need the installed application, not only PyInstaller's --paths.
            wheel_dir = receipt_dir / "wheel"
            run(
                "app-wheel",
                [
                    uv,
                    "--no-config",
                    "build",
                    "--wheel",
                    "--no-build-isolation",
                    "--python",
                    python,
                    "--out-dir",
                    wheel_dir,
                    ROOT,
                ],
            )
            wheels = list(wheel_dir.glob("*.whl"))
            if len(wheels) != 1:
                raise ValueError("Expected one application wheel")
            result["application_wheel_sha256"] = sha(wheels[0])
            run(
                "app-install",
                [uv, "--no-config", "pip", "install", "--python", python, "--no-deps", wheels[0]],
            )
            run(
                "python-identity",
                [
                    python,
                    "-c",
                    "import json,sys,chatbot,importlib.metadata as m; print(json.dumps({'python':sys.version,'executable':sys.executable,'application':chatbot.__file__,'packages':{d.metadata['Name']:d.version for d in m.distributions()}},sort_keys=True))",
                ],
            )
            targets = (
                ("GameBackend", "SceneweaverBackend", "Game", "Sceneweaver")
                if args.target == "all"
                else ()
                if args.target == "prepare"
                else (args.target,)
            )
            if any(name in {"GameBackend", "SceneweaverBackend", "SceneweaverBackendP0"} for name in targets):
                run("npm-ci", [shutil.which("npm.cmd") or "npm", "ci"], cwd=ROOT / "apps/web")
            for name in targets:
                backend = name in {"GameBackend", "SceneweaverBackend", "SceneweaverBackendP0"}
                command = [
                    python,
                    HERE / "build_frozen.py",
                    "--noconfirm",
                    "--clean",
                    "--noupx",
                    "--console" if backend else "--windowed",
                    "--onedir",
                    "--name",
                    name,
                    "--paths",
                    ROOT / "src",
                    "--distpath",
                    output / "dist",
                    "--workpath",
                    output / ("build-" + name),
                    "--specpath",
                    output / "specs",
                ]
                if backend:
                    folder, entry, task = (
                        ("dist-game", "game.html", "build:game")
                        if name == "GameBackend"
                        else ("dist", "index.html", "build")
                    )
                    run("web-" + name, [shutil.which("npm.cmd") or "npm", "run", task], cwd=ROOT / "apps/web")
                    if not (ROOT / "apps/web" / folder / entry).is_file():
                        raise ValueError("Frontend build entry missing")
                    command += [
                        "--add-data",
                        str(ROOT / "apps/web" / folder) + ";apps/web/" + folder,
                        "--add-data",
                        str(ROOT / "src/chatbot/items/presets.v1.json") + ";chatbot/items",
                        "--collect-data",
                        "jieba",
                        "--recursive-copy-metadata",
                        "langgraph",
                    ]
                command.append(HERE / TARGETS[name])
                run("freeze-" + name, command)
                (output / "dist" / name / "LICENSE").write_bytes((ROOT / "LICENSE").read_bytes())
                if name == "GameBackend":
                    run(
                        "capabilities",
                        [
                            python,
                            ROOT / "scripts/write_game_runtime_capabilities.py",
                            output / "dist/GameBackend/sceneweaver-runtime-capabilities.json",
                        ],
                    )
                result.setdefault("components", {})[name] = {
                    "executable_sha256": sha(output / "dist" / name / (name + ".exe"))
                }
        result["completed"] = True
    finally:
        (receipt_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n", "utf-8")


if __name__ == "__main__":
    main()
