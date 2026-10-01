"""Compose a native game's release loader, player API and scoped worker."""

from .experiment_initials import digest
from .game_library import create_game_library
from .game_scope import GameExecutionScope
from .game_seed import load_game_seed
from .storage import ExperimentInitial
from .worker import Worker


async def create_native_game(config, db, *, package_path, web_dir, credentials_path):
    source = await load_game_seed(db, config, package_path)
    with db.session() as s:
        release = digest(s.get(ExperimentInitial, source).recipe)
    worker = Worker(db, config, execution_scope=GameExecutionScope(config.workspace_owner, release))
    try:
        app = create_game_library(
            config,
            db,
            worker.provider,
            source_story_id=source,
            game_web_dir=web_dir,
            player_credentials_path=credentials_path,
        )
    except BaseException:
        await worker.provider.close()
        raise
    return app, worker
