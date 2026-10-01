import argparse
import asyncio
import logging
import sys

from .config import settings
from .storage import Database


def main():
    parser = argparse.ArgumentParser(description="sceneweaver NPC prototype")
    parser.add_argument("command", choices=["init", "serve", "worker", "doctor", "reindex", "project-events"])
    args = parser.parse_args()
    config = settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    db = Database(config)
    if args.command == "init":
        from alembic import command
        from alembic.config import Config

        command.upgrade(Config("alembic.ini"), "head")
        print("Database schema and pgvector are ready.")
    elif args.command == "project-events":
        import json

        from sqlalchemy import select

        from .event_projection import backfill_story, projected_exchanges
        from .observations import visible_records
        from .scene_state import exchanges
        from .storage import Conversation, Story

        with db.session() as s:
            stories = list(s.scalars(select(Story).where(Story.owner_id == config.workspace_owner)))
        processed = mismatches = viewers = 0
        differences = []
        for story in stories:
            after = 0
            while after < story.seq:
                with db.session.begin() as s:
                    page = backfill_story(s, story.id, after=after, through=story.seq)
                processed += page["processed"]
                if page["after"] <= after:
                    break
                after = page["after"]
            # One snapshot prevents a concurrent commit from producing a false mismatch.
            with db.session.begin() as s:
                locked = s.scalar(select(Story).where(Story.id == story.id).with_for_update())
                previous_mismatches = mismatches
                for actor in s.scalars(select(Conversation).where(Conversation.story_id == story.id)):
                    viewers += 1
                    projected = projected_exchanges(s, actor.id)
                    legacy = exchanges(visible_records(s, actor.id))
                    if projected != legacy:
                        mismatches += 1
                        keys = ("question_id", "asker_id", "target_id", "status", "response_event_id")
                        differences.append(
                            {
                                "actor_id": actor.id,
                                "legacy": [{k: r[k] for k in keys} for r in legacy],
                                "projected": [{k: r[k] for k in keys} for r in projected],
                            }
                        )
                if mismatches == previous_mismatches:
                    locked.settings = {**locked.settings, "request_projection_version": 1}
        print(
            json.dumps(
                {
                    "stories": len(stories),
                    "processed": processed,
                    "viewers": viewers,
                    "mismatches": mismatches,
                    "differences": differences,
                }
            )
        )
        if mismatches:
            raise SystemExit(1)
    elif args.command == "doctor":
        from sqlalchemy import text

        with db.engine.connect() as c:
            print("PostgreSQL:", c.scalar(text("SELECT version()")))
            print("pgvector:", c.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'")))
        print("Generation credential configured:", bool(config.generation_key()))
        print("Semantic search:", config.embedding_provider)
        print("Daily API cap USD:", config.daily_budget_usd)
    elif args.command == "reindex":
        from pathlib import Path

        from sqlalchemy import select

        from .knowledge import Knowledge
        from .runtime import Runtime
        from .storage import Document

        knowledge = Knowledge(db, config, Runtime(db, config), None)
        with db.session() as s:
            documents = list(
                s.scalars(
                    select(Document).where(
                        Document.owner_id == config.workspace_owner, Document.deleted.is_(False)
                    )
                )
            )
        for document in documents:
            knowledge.upload(
                config.workspace_owner, document.name, Path(document.blob_path).read_bytes(), document.id
            )
        print(f"Queued {len(documents)} document versions; run worker to rebuild indexes.")
    elif args.command == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(config, db), host=config.bind_host, port=config.port, access_log=False)
    else:
        from .worker import Worker

        try:
            asyncio.run(Worker(db, config).run())
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
