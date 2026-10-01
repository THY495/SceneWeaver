"""Exercise a frozen game API in fresh storage without credentials or model calls."""

import argparse
import json
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("release", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    release, output = args.release.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    control = output / "control"
    report = {}
    with (output / "backend.log").open("wb") as log:
        process = subprocess.Popen(
            [
                str(release / "GameBackend/GameBackend.exe"),
                str(release / "pgsql"),
                str(output / "world"),
                str(output / "aliases"),
                str(control),
                "--online",
                "--game-package",
                str(release / "story.json"),
                "--lifetime",
                "90",
            ],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            deadline = time.monotonic() + 45
            while not (control / "ready.json").exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Backend failed to become ready; inspect local backend.log")
                time.sleep(0.1)
            ready = json.loads((control / "ready.json").read_text())

            def request(path, *, authenticated=True, body=None, method=None):
                headers = {"Content-Type": "application/json"}
                if authenticated:
                    headers["Authorization"] = "Bearer " + ready["token"]
                req = Request(
                    f"http://127.0.0.1:{ready['port']}" + path,
                    headers=headers,
                    data=None if body is None else json.dumps(body).encode(),
                    method=method,
                )
                try:
                    response = urlopen(req, timeout=10)
                except HTTPError as exc:
                    response = exc
                with response:
                    raw = response.read()
                    return response.status, json.loads(raw) if raw else None

            status, credentials = request("/api/player/credentials")
            assert status == 200 and credentials["available"]
            assert not credentials["llm_configured"] and not credentials["tts_configured"]
            report["empty_credentials"] = True
            assert request("/api/player/credentials", authenticated=False)[0] == 401
            report["unauthenticated_credentials_rejected"] = True
            assert request("/api/role/experiments/blueprints")[0] == 404
            report["author_routes_absent"] = True
            status, result = request("/api/game/saves", body={"request_id": "missing-key-probe"})
            assert status in (409, 503), (status, result)
            report["new_game_without_key"] = {"status": status, "detail": result}
            status, catalogue = request("/api/game/library")
            assert status == 200 and catalogue["saves"] == []
            report["no_save_created"] = True
            # Synthetic values are never checked against a provider or used to start a save.
            assert (
                request(
                    "/api/player/credentials",
                    method="PATCH",
                    body={"llm_key": "synthetic-probe-llm", "tts_key": "synthetic-probe-tts"},
                )[0]
                == 200
            )
            assert (
                request(
                    "/api/player/credentials",
                    method="PATCH",
                    body={"tts_voice": "longanfengyue", "daily_budget_usd": 1.5},
                )[0]
                == 200
            )
            _, status = request("/api/player/credentials")
            assert status["llm_configured"] and status["tts_configured"]
            assert status["daily_budget_usd"] == 1.5
            report["partial_update_preserves_configured_keys"] = True
            assert (
                request("/api/player/credentials", method="PATCH", body={"llm_key": "", "tts_key": ""})[0]
                == 200
            )
            _, status = request("/api/player/credentials")
            assert not status["llm_configured"] and not status["tts_configured"]
            report["explicit_clear"] = True
        finally:
            if control.exists():
                (control / "stop").touch()
            try:
                process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                process.kill()  # Only this probe's owned backend; its Job owns children.
                process.wait(timeout=10)
                raise RuntimeError("Backend did not stop within 25 seconds") from None
        assert process.returncode == 0
        report["exit_code"] = process.returncode
        report["closed"] = json.loads((control / "backend-closed.json").read_text())
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
