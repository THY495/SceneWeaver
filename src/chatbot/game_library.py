"""One-work release menu with independently scoped per-save game applications."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import iter_route_contexts
from fastapi.staticfiles import StaticFiles

from .api import create_app
from .game_api import create_game_app
from .game_saves import GameSaves, NewGame
from .native_paths import filesystem_path


def create_game_library(
    config, db, provider=None, *, source_story_id, game_web_dir, player_credentials_path=None
):
    web = filesystem_path(Path(game_web_dir))
    if not (web / "game.html").is_file() or not (web / "assets").is_dir():
        raise ValueError("Build the independent game first")
    app = create_app(config, db, provider, serve_web=False)
    library = GameSaves(db, config, app.state.runtime, source_story_id=source_story_id)
    app.state.game_saves = library
    # Root exposes model readiness and the catalogue, never a mutable active player.
    routes = list(iter_route_contexts(app.routes))
    app.router.routes.clear()
    for route in routes:
        if route.path in {"/api/runtime/readiness", "/api/runtime/check"}:
            app.add_api_route(route.path, route.endpoint, methods=route.methods, include_in_schema=False)
    auth = [Depends(app.state.owner_dependency)]

    @app.get("/api/game/library", dependencies=auth)
    def catalogue():
        return {"title": library.title, "saves": library.list()}

    @app.post("/api/game/saves", dependencies=auth, status_code=201)
    async def new_game(body: NewGame):
        return await library.create(body)

    children = {}
    if player_credentials_path is not None:
        from .player_credentials import install

        install(app, config, db, Path(player_credentials_path))

    async def dispatch(scope, receive, send):
        if scope["type"] != "http":
            return
        player_id = scope["path_params"]["player_id"]
        try:
            with db.session() as s:
                library.owned(s, player_id)
        except HTTPException:
            await JSONResponse({"detail": "存档不存在。"}, status_code=404)(scope, receive, send)
            return
        if player_id not in children:
            children[player_id] = create_game_app(
                config,
                db,
                app.state.agents.provider,
                player_id=player_id,
                game_web_dir=web,
            )
            children[player_id].state.runtime.model_readiness = app.state.runtime.model_readiness
            if player_credentials_path is not None:
                install(children[player_id], config, db, Path(player_credentials_path))
        await children[player_id](scope, receive, send)

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(host):
        async with original_lifespan(host):
            try:
                yield
            finally:
                for child in children.values():
                    await child.state.voice.close()

    app.router.lifespan_context = lifespan
    app.mount("/runs/{player_id}", dispatch, name="game-save")
    app.mount("/assets", StaticFiles(directory=web / "assets"), name="game-assets")

    @app.get("/", include_in_schema=False)
    def menu():
        return FileResponse(web / "game.html")

    return app
