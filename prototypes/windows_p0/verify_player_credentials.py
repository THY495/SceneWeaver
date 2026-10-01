"""Native host restart test with synthetic keys; never call a model endpoint."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pgroot", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--host-exe", type=Path)
    parser.add_argument("--game-package", type=Path)
    args = parser.parse_args()
    pg = args.pgroot.resolve(strict=True)
    entry = ([str(args.host_exe.resolve(strict=True))] if args.host_exe else
             [sys.executable, str(Path(__file__).with_name("backend_host.py"))])
    package = args.game_package.resolve(strict=True) if args.game_package else None
    run = args.work.resolve() / ("byok-" + uuid4().hex[:8])
    run.mkdir(parents=True)
    report = {"real_provider_called": False, "frozen": bool(args.host_exe),
              "game_mode": bool(package), "run": str(run), "checks": []}
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "LOCALAPPDATA", "USERPROFILE"}
    }
    if not args.host_exe:
        env["PYTHONPATH"] = str(root / "src")
    previous_token = None
    first_catalogue = None
    for index in range(2):
        control = run / str(index)
        control.mkdir()
        with (control / "host.log").open("wb") as log:
            child = subprocess.Popen(
                [
                    *entry,
                    str(pg),
                    str(run / "world"),
                    str(run / "aliases"),
                    str(control),
                    "--online",
                    "--lifetime",
                    "60",
                    *(["--game-package", str(package)] if package else []),
                ],
                env=env,
                cwd=control,
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                for _ in range(200):
                    if (control / "ready.json").exists():
                        break
                    if child.poll() is not None:
                        raise RuntimeError("Native host stopped before readiness; inspect local log")
                    time.sleep(0.1)
                else:
                    raise TimeoutError("Native host startup timed out")
                ready = json.loads((control / "ready.json").read_text())
                assert ready["fake_model"] is False
                assert ready["game_mode"] == bool(package)
                if previous_token:
                    assert ready["token"] != previous_token
                with httpx.Client(base_url=f"http://127.0.0.1:{ready['port']}", trust_env=False) as client:
                    assert client.get("/api/player/credentials").status_code == 401
                    if previous_token:
                        assert client.get("/api/player/credentials", headers={
                            "Authorization": "Bearer " + previous_token
                        }).status_code == 401
                    headers = {"Authorization": "Bearer " + ready["token"]}
                    state = client.get("/api/player/credentials", headers=headers).json()
                    assert state["llm_configured"] == bool(index)
                    assert not state["tts_configured"]
                    if package:
                        catalogue = client.get("/api/game/library", headers=headers)
                        catalogue.raise_for_status()
                        value = catalogue.json()
                        assert value["title"] == json.loads(package.read_text("utf-8"))["recipe"]["title"]
                        assert value["saves"] == []
                        if index:
                            assert value == first_catalogue
                        first_catalogue = value
                        for path in ("/api/projects", "/api/characters", "/api/role/stories"):
                            assert client.get(path, headers=headers).status_code == 404
                        page = client.get("/")
                        assert page.status_code == 200
                        assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', page.text)
                        assert assets and all(client.get(asset).status_code == 200 for asset in assets)
                        if not index:
                            rejected = client.post("/api/game/saves", headers=headers,
                                                   json={"request_id": "no-key", "label": "不得创建"})
                            assert rejected.status_code == 409
                            assert client.get("/api/game/library", headers=headers).json()["saves"] == []
                            report["checks"].append("missing_llm_key_cannot_create_save")
                    if index == 0:
                        result = client.put(
                            "/api/player/credentials",
                            headers=headers,
                            json={"llm_key": "synthetic-native-key", "tts_key": ""},
                        )
                        assert result.status_code == 200
                        assert (
                            b"synthetic-native-key"
                            not in (run / "world/player-credentials.dpapi").read_bytes()
                        )
                    assert (
                        "synthetic-native-key"
                        not in client.get("/api/player/credentials", headers=headers).text
                    )
                    report["checks"].append(
                        "first_save_encrypted" if index == 0 else "restart_restored_without_secret_echo"
                    )
                previous_token = ready["token"]
            finally:
                (control / "stop").touch()
                try:
                    assert child.wait(timeout=25) == 0
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                    raise TimeoutError("Native host failed to stop normally") from None
                closed = json.loads((control / "backend-closed.json").read_text())
                assert closed["fake_calls"] == 0 and closed["checkpoints"] == 0
    report["checks"].append("new_instance_token_rejects_previous_token")
    if package:
        report["checks"].append("release_title_assets_and_editor_isolation_survive_restart")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
