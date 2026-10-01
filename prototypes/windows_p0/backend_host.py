"""Windows API/Worker host: offline fixture by default, opt-in player BYOK."""

import argparse
import asyncio
import json
import os
import secrets
import socket
import sys
from pathlib import Path

import uvicorn
from fastapi.responses import HTMLResponse, JSONResponse
from managed_host import DatabaseHost, InstanceLock, atomic_json, runtime_path
from pydantic import SecretStr
from sqlalchemy import text
from windows_job import contain_current_host, process_ids

from chatbot.api import create_app
from chatbot.config import Settings
from chatbot.storage import Database
from chatbot.worker import Worker


class OfflineSettings(Settings):
    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        return (init_settings,)  # Never inherit workspace credentials or service URLs.


class OfflineProvider:
    def __init__(self):
        self.calls = 0

    async def chat(self, task_id, role, messages, **kwargs):
        self.calls += 1
        schema = kwargs.get("schema")
        values = {
            "MemoryExtraction": {"candidates": []},
            "SearchPlan": {"queries": [], "sufficient": True, "missing": []},
            "Route": {"route": "direct", "reason": "P0 offline fixture"},
            "ActorReview": {"accepted": True, "issues": [], "delivery": {}},
            "ActorIntent": {"act": "speak", "purpose": "回应", "source_ids": [], "wait_for_player": True},
            "ActorSpeech": {"text": "【P0 假模型】我听到了，我们可以继续交谈。", "claims": []},
            "RoleReflection": {"summary": "P0 离线测试记忆", "notes": []},
        }
        if schema:
            if schema.__name__ not in values:
                raise ValueError(f"P0 fixture does not support schema {schema.__name__}")
            return schema.model_validate(values[schema.__name__]).model_dump()
        result = "【P0 假模型】我听到了，我们可以继续交谈。"
        if kwargs.get("on_delta"):
            await kwargs["on_delta"](result)
        return result

    async def close(self):
        pass


async def serve(host, control, web, job, lifetime=120, online=False, game_package=None, game_runtime=None):
    if game_package is not None and not online:
        raise ValueError("Standalone games require player BYOK mode")
    if game_package is not None and game_runtime is not None:
        raise ValueError("Game hosts cannot install authoring build runtimes")
    token = secrets.token_urlsafe(32)
    ticket = secrets.token_urlsafe(32)
    ticket_used = False
    # Keep the HTTP socket reserved through server startup; no free-port race.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    config = OfflineSettings(
        _env_file=None,
        product="roleplay",
        workspace_owner="p0",
        database_url=f"postgresql+psycopg://probe:{host.password}@127.0.0.1:{host.port}/postgres",
        data_dir=host.directory / "app-data",
        port=port,
        access_token=token,
        model_provider="p0-offline",
        dialogue_model="p0-fake",
        expert_model="p0-fake",
        embedding_provider="lexical",
        acknowledgement_enabled=False,
        tts_enabled=False,
        windows_game_runtime_dir=game_runtime,
    )
    config.data_dir.mkdir(exist_ok=True)
    if online:
        from chatbot.player_credentials import apply, load

        config.model_provider = "deepseek"
        config.dialogue_model = "deepseek-v4-flash"
        config.expert_model = "deepseek-v4-pro"
        apply(config, load(host.directory / "player-credentials.dpapi"))
    else:
        config.api_key = SecretStr("p0-offline-test-only")
    db = Database(config)
    db.initialize()
    if game_package is not None:
        from chatbot.native_game import create_native_game

        app, worker = await create_native_game(
            config,
            db,
            package_path=game_package,
            web_dir=web,
            credentials_path=host.directory / "player-credentials.dpapi",
        )
    else:
        worker = Worker(db, config, None if online else OfflineProvider())
        app = create_app(config, db, worker.provider, web_dir=web)
    provider = worker.provider
    if online and game_package is None:
        from chatbot.player_credentials import install

        install(app, config, db, host.directory / "player-credentials.dpapi")

    # Insert before the existing SPA catch-all. One-use capability hands the
    # instance token to this tab before React starts, without a Python JS bridge.
    async def bootstrap(value: str):
        nonlocal ticket_used
        if ticket_used or not secrets.compare_digest(value, ticket):
            return JSONResponse({"detail": "launch link expired"}, status_code=403)
        ticket_used = True
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><title>Loading</title><script>"
            + "sessionStorage.setItem('continuum-token',"
            + json.dumps(token)
            + ");"
            + "history.replaceState(null,'','/');location.replace('/');</script>",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    app.add_api_route("/p0-launch/{value}", bootstrap, methods=["GET"], include_in_schema=False)
    app.router.routes.insert(0, app.router.routes.pop())

    @app.middleware("http")
    async def local_boundary(request, call_next):
        if request.headers.get("host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            return JSONResponse({"detail": "invalid host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            return JSONResponse({"detail": "invalid origin"}, status_code=403)
        if request.url.path.startswith(("/api/", "/docs", "/openapi.json", "/redoc")):
            if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
                return JSONResponse({"detail": "authentication required"}, status_code=401)
        return await call_next(request)

    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, loop="asyncio"))
    server_task = None
    worker_task = asyncio.create_task(worker.run())
    try:
        for _ in range(200):
            if worker_task.done():
                await worker_task
                raise RuntimeError("Worker ended during startup")
            if worker.graph is not None:
                break
            await asyncio.sleep(0.05)
        else:
            raise TimeoutError("Worker checkpoint initialization timed out")
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        for _ in range(200):
            if server_task.done():
                await server_task
                raise RuntimeError("HTTP server ended during startup")
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:
            raise TimeoutError("HTTP startup timed out")
        atomic_json(
            control / "ready.json",
            {
                "port": port,
                "token": token,
                "launch_url": f"http://127.0.0.1:{port}/p0-launch/{ticket}",
                "fake_model": not online,
                "game_mode": game_package is not None,
                "host_pid": os.getpid(),
                "pids": process_ids(job),
            },
        )
        deadline = asyncio.get_running_loop().time() + lifetime if lifetime else float("inf")
        while not (control / "stop").exists() and asyncio.get_running_loop().time() < deadline:
            if worker_task.done():
                await worker_task
                raise RuntimeError("Worker stopped unexpectedly")
            if server_task.done():
                await server_task
                break
            await asyncio.sleep(0.1)
    finally:
        if server_task:
            server.should_exit = True
            await asyncio.wait_for(server_task, 10)
        worker.stop.set()
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
        with db.session() as session:
            checkpoints = session.execute(text("SELECT count(*) FROM checkpoints")).scalar()
        atomic_json(
            control / "backend-closed.json",
            {"fake_calls": getattr(provider, "calls", 0), "checkpoints": checkpoints},
        )
        db.close()
        listener.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pgroot", type=Path)
    parser.add_argument("directory", type=Path)
    parser.add_argument("alias_root", type=Path)
    parser.add_argument("control", type=Path)
    parser.add_argument("--web", type=Path)
    parser.add_argument("--game-package", type=Path)
    parser.add_argument("--game-runtime", type=Path, help="Studio-only installed Windows build runtime")
    parser.add_argument("--lifetime", type=int, default=120)
    parser.add_argument(
        "--online", action="store_true", help="Use player-local credentials; never inherit .env"
    )
    args = parser.parse_args()
    if args.game_package and not args.online:
        parser.error("--game-package requires --online (player-local BYOK)")
    if args.game_package and args.game_runtime:
        parser.error("--game-runtime is only available to the development tool")
    job = contain_current_host()
    args.directory.mkdir(parents=True, exist_ok=True)
    args.control.mkdir(parents=True, exist_ok=True)
    web = args.web or Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / (
        "apps/web/dist-game" if args.game_package else "apps/web/dist"
    )
    if not (web / ("game.html" if args.game_package else "index.html")).is_file():
        raise FileNotFoundError("Built frontend missing")
    with InstanceLock(args.directory):
        host = DatabaseHost(runtime_path(args.pgroot, args.alias_root), args.directory)
        try:
            host.initialize()
            host.start()
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            asyncio.run(
                serve(
                    host,
                    args.control,
                    web,
                    job,
                    args.lifetime,
                    args.online,
                    args.game_package,
                    args.game_runtime,
                )
            )
        finally:
            host.stop()


if __name__ == "__main__":
    main()
