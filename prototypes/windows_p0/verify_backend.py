"""Exercise production HTTP, Worker and checkpoints across host restarts."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pgroot", type=Path)
    parser.add_argument("work", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--host-exe", type=Path)
    args = parser.parse_args()
    run = args.work.resolve() / ("backend-" + uuid4().hex[:8])
    run.mkdir(parents=True)
    if args.host_exe:
        original = args.host_exe.resolve()
        copied = run / "独立发行 文件夹"
        shutil.copytree(original.parent, copied)
        args.host_exe = copied / original.name
    processes = []
    checks = []

    def start():
        control = run / ("control-" + uuid4().hex[:8])
        control.mkdir()
        log = (control / "output.log").open("wb")
        entry = (
            [str(args.host_exe.resolve())]
            if args.host_exe
            else [sys.executable, str(Path(__file__).resolve().with_name("backend_host.py"))]
        )
        env = {
            k: v
            for k, v in os.environ.items()
            if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "LOCALAPPDATA", "USERPROFILE"}
        }
        env["PATH"] = str(Path(os.environ["SYSTEMROOT"]) / "System32")
        # Deliberately invalid synthetic configuration, not user credentials.
        env["CHAT_DATABASE_URL"] = "invalid-environment-database"
        env["CHAT_API_KEY_FILE"] = str(control / "nonexistent-secret-file")
        (control / ".env").write_text(
            "CHAT_PORT=not-a-port\nCHAT_DATABASE_URL=invalid-dotenv\n", encoding="utf-8"
        )
        if not args.host_exe:
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
        child = subprocess.Popen(
            entry + [str(args.pgroot.resolve()), str(run / "作品数据"), str(run / "aliases"), str(control)],
            env=env,
            cwd=control,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        processes.append((child, control, log))
        deadline = time.monotonic() + 40
        while not (control / "ready.json").exists():
            if child.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(f"Backend startup failed; inspect {control / 'output.log'}")
            time.sleep(0.1)
        state = json.loads((control / "ready.json").read_text())
        client = httpx.Client(
            base_url=f"http://127.0.0.1:{state['port']}",
            headers={"Authorization": "Bearer " + state["token"]},
            timeout=10,
            trust_env=False,
        )
        launch = client.get(state["launch_url"])
        assert launch.status_code == 200 and launch.headers["cache-control"] == "no-store"
        assert "sessionStorage.setItem" in launch.text
        assert json.dumps(state["token"]) in launch.text
        assert client.get(state["launch_url"]).status_code == 403
        return child, control, client

    def request(client, method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    def stop(child, control, client):
        client.close()
        (control / "stop").touch()
        assert child.wait(timeout=25) == 0
        return json.loads((control / "backend-closed.json").read_text())

    try:
        child, control, client = start()
        assert request(client, "GET", "/api/health")["status"] == "ok"
        configuration = request(client, "GET", "/api/config")
        assert configuration["provider"] == "p0-offline" and not configuration["generation_configured"]
        checks.append(
            "invalid synthetic environment/dotenv settings are ignored; no generation key configured"
        )
        assert client.get("/api/sessions", headers={"Authorization": ""}).status_code == 401
        assert client.get("/api/sessions", headers={"Origin": "https://example.invalid"}).status_code == 403
        assert client.get("/api/health", headers={"Host": "example.invalid"}).status_code == 403
        checks.append("real HTTP API requires instance bearer token and rejects foreign origin/host")
        index = client.get("/")
        assert index.status_code == 200
        assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', index.text)
        assert assets and all(client.get(asset).status_code == 200 for asset in assets)
        checks.append("built frontend index and referenced entry assets are served")
        card = request(
            client,
            "POST",
            "/api/characters",
            json={"name": "摆渡人", "core": "P0 离线人物，只用于打包验证。"},
        )
        session = request(
            client,
            "POST",
            f"/api/characters/{card['id']}/sessions",
            json={"version": 1, "request_id": "p0-start"},
        )
        sid = session["id"]
        body = {"content": "你好", "client_message_id": "p0-message", "mode": "direct"}
        task = request(client, "POST", f"/api/sessions/{sid}/messages", json=body)
        deadline = time.monotonic() + 30
        while True:
            snapshot = request(client, "GET", f"/api/sessions/{sid}")
            current = next(t for t in snapshot["tasks"] if t["id"] == task["task_id"])
            if current["status"] == "completed":
                break
            assert current["status"] not in {"failed", "superseded"}, current
            if time.monotonic() > deadline:
                raise TimeoutError("Dialogue did not finish")
            time.sleep(0.1)
        answers = [m for m in snapshot["messages"] if m["role"] == "assistant"]
        assert len(answers) == 1 and "P0 假模型" in answers[0]["content"]
        checks.append("production character API → message → Worker/Agents → persisted fake reply")
        first = stop(child, control, client)
        assert first["checkpoints"] > 0 and first["fake_calls"] > 0
        child, control, client = start()
        restored = request(client, "GET", f"/api/sessions/{sid}")
        assert restored["messages"] == snapshot["messages"]
        replay = request(client, "POST", f"/api/sessions/{sid}/messages", json=body)
        assert replay["task_id"] == task["task_id"]
        after = request(client, "GET", f"/api/sessions/{sid}")
        assert after["messages"] == restored["messages"]
        second = stop(child, control, client)
        assert second["checkpoints"] >= first["checkpoints"]
        checks.append(
            "restart restores identical messages/checkpoints; duplicate request reuses task without duplicate reply"
        )
        child, control, client = start()
        scenario = request(
            client,
            "POST",
            "/api/role/experiments/start",
            json={"request_id": "p0-player", "template_id": "partial-hearing"},
        )
        player = scenario["player_id"]
        request(
            client,
            "PUT",
            f"/api/role/stories/{scenario['story_id']}/settings",
            json={"paused": False, "idle_enabled": False, "scene_enabled": True},
        )
        view = request(client, "GET", f"/api/role/players/{player}/snapshot")
        assert [p["name"] for p in view["characters"]] == ["许遥"]
        assert "钥匙在暗格" not in json.dumps(view, ensure_ascii=False)
        player_task = request(
            client,
            "POST",
            f"/api/sessions/{player}/messages",
            json={"content": "许遥，你好。", "client_message_id": "p0-player-message"},
        )
        deadline = time.monotonic() + 30
        while True:
            state = request(client, "GET", f"/api/sessions/{player}")
            current = next(t for t in state["tasks"] if t["id"] == player_task["task_id"])
            if current["status"] == "completed":
                break
            assert current["status"] not in {"failed", "superseded"}, current
            if time.monotonic() > deadline:
                raise TimeoutError("Player-mode task did not finish")
            time.sleep(0.1)
        assert current["result"]["actor_ids"] == [scenario["actors"]["reporter"]]
        view = request(client, "GET", f"/api/role/players/{player}/snapshot")
        assert "钥匙在暗格" not in json.dumps(view, ensure_ascii=False)
        from transfer_checks import verify_transfers

        transfers = verify_transfers(client)
        player_close = stop(child, control, client)
        child, control, client = start()
        for transfer in transfers:
            snapshot = request(client, "GET", f"/api/role/players/{transfer['player_id']}/snapshot")
            assert snapshot["inventory"]["items"][0]["holder_id"] == transfer["holder_id"]
            assert snapshot["inventory"]["transfers"][0]["state"] == transfer["transfer_state"]
        stop(child, control, client)
        checks.append(
            "handoff blocks before consent; acceptance transfers once; refusal preserves ownership; both survive restart"
        )
        checks.append(
            "player-mode reply comes from co-located reporter; offstage private speech absent from player snapshot"
        )
        result = {
            "status": "passed_on_developer_host",
            "frozen_host": bool(args.host_exe),
            "checks": checks,
            "first_close": first,
            "second_close": second,
            "player_close": player_close,
            "transfers": transfers,
            "limitations": [
                "one character dialogue and one player-mode reply; not a full multi-NPC playthrough",
                "assets fetched over HTTP, no browser UI interaction",
                "no paid model or clean-machine validation",
            ],
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
    finally:
        for child, control, log in processes:
            if child.poll() is None:
                (control / "stop").touch()
                child.wait(timeout=50)
            log.close()


if __name__ == "__main__":
    main()
