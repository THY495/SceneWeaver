"""Player-only API for a host-bound run; never serve the Studio web bundle.

The host selects the run, not a caller-provided actor id. Game startup/save-slot
selection and the independent web bundle are separate host responsibilities.
New Studio routes are excluded by default, even when they share a URL prefix.
"""

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.staticfiles import StaticFiles

from .api import create_app
from .native_paths import filesystem_path
from .storage import Conversation, Story, Task, VoicePlayback

# Exact route templates and methods, not permissive prefix matching.
PLAYER_ROUTES = {
    ("POST", "/api/role/presentation/player-videos/{actor_id}/{node_id}/ticket"),
    ("GET", "/api/role/presentation/player-video-stream/{actor_id}/{node_id}"),
    ("GET", "/api/role/presentation/player-videos/{actor_id}/{node_id}"),
    ("GET", "/api/runtime/readiness"),
    ("POST", "/api/runtime/check"),
    ("GET", "/api/role/players/{aid}/snapshot"),
    ("GET", "/api/role/players/{aid}/reading"),
    ("PUT", "/api/role/players/{aid}/reading"),
    ("GET", "/api/role/items/actors/{aid}"),
    ("POST", "/api/role/items/actors/{aid}/actions"),
    ("POST", "/api/role/items/actors/{aid}/offers"),
    ("POST", "/api/sessions/{sid}/messages"),
    ("POST", "/api/tasks/{tid}/cancel"),
    ("GET", "/api/voice/config"),
    ("POST", "/api/voice/sessions/{sid}/warm"),
    ("GET", "/api/voice/sessions/{sid}/utterances"),
    ("POST", "/api/voice/sessions/{sid}/utterances/{eid}/visible"),
    ("POST", "/api/voice/sessions/{sid}/playbacks"),
    ("GET", "/api/voice/playbacks/{pid}"),
    ("POST", "/api/voice/playbacks/{pid}/start"),
    ("POST", "/api/voice/playbacks/{pid}/receipt"),
    ("POST", "/api/voice/playbacks/{pid}/audio"),
}

PLAYER_ALIASES = {
    ("POST", "/api/role/stories/{sid}/presence"): "/api/game/stories/{sid}/presence",
    **{
        (
            "POST",
            f"/api/role/experiments/stories/{{sid}}/narrative/{action}",
        ): f"/api/game/stories/{{sid}}/narrative/{action}"
        for action in ("choose", "advance", "auto-continue")
    },
}


def create_game_app(config, db, provider=None, *, player_id: str, game_web_dir=None):
    if config.product != "roleplay" or not player_id:
        raise ValueError("A roleplay game must bind an existing player run")

    def scope(request: Request):
        with db.session() as s:
            player = s.get(Conversation, player_id)
            story = s.get(Story, player.story_id) if player and player.story_id else None
            if (
                not player
                or player.owner_id != config.workspace_owner
                or not player.current_state.get("player")
                or not story
                or story.owner_id != config.workspace_owner
            ):
                raise HTTPException(404)
            params = request.path_params
            story_route = request.scope["route"].path.startswith("/api/game/stories/")
            if story_route and params.get("sid") != story.id:
                raise HTTPException(404)
            actor_keys = ("aid", "actor_id") if story_route else ("aid", "sid", "actor_id")
            if any(params[k] != player_id for k in actor_keys if k in params):
                raise HTTPException(404)
            for key, model in (("pid", VoicePlayback), ("tid", Task)):
                if key in params:
                    row = s.get(model, params[key])
                    if not row or row.session_id != player_id:
                        raise HTTPException(404)

    app = create_app(config, db, provider, serve_web=False)
    source_routes = list(iter_route_contexts(app.routes))
    # Remove documentation, Studio API, and any fallback route. Re-registering
    # builds FastAPI's dependency graph with the scope guard on every endpoint.
    app.router.routes.clear()
    app.openapi_schema = None
    for route in source_routes:
        if not isinstance(route.original_route, APIRoute):
            continue
        methods = {
            m for m in route.methods if (m, route.path) in PLAYER_ROUTES or (m, route.path) in PLAYER_ALIASES
        }
        if methods:
            path = next(
                (PLAYER_ALIASES[(m, route.path)] for m in methods if (m, route.path) in PLAYER_ALIASES),
                route.path,
            )
            app.add_api_route(
                path,
                route.endpoint,
                methods=methods,
                dependencies=[Depends(scope), *route.dependencies],
                response_model=route.response_model,
                status_code=route.status_code,
                response_class=route.response_class,
                name=route.name,
                include_in_schema=False,
            )
    from .game_controls import install_controls

    install_controls(app, db, config, player_id, scope)
    if game_web_dir is not None:
        from pathlib import Path

        web = filesystem_path(Path(game_web_dir))
        if not (web / "game.html").is_file() or not (web / "assets").is_dir():
            raise ValueError("Expected an independent game build, not the Studio bundle")
        app.mount("/assets", StaticFiles(directory=web / "assets"), name="game-assets")

        @app.get("/", include_in_schema=False)
        def game_page():
            return FileResponse(web / "game.html")

    return app
