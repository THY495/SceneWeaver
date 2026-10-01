import asyncio
import os
import sys

import pytest
from sqlalchemy import create_engine, text
from support.providers import FakeProvider as FakeProvider

from chatbot.config import Settings
from chatbot.runtime import Runtime
from chatbot.storage import Base, Database


@pytest.fixture(scope="session")
def event_loop_policy():
    return (
        asyncio.WindowsSelectorEventLoopPolicy()
        if sys.platform == "win32"
        else asyncio.DefaultEventLoopPolicy()
    )


@pytest.fixture
def config(tmp_path):
    return Settings(
        _env_file=None,
        database_url=os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql+psycopg://sceneweaver:sceneweaver_local@127.0.0.1:55432/sceneweaver_test",
        ),
        data_dir=tmp_path,
        api_key="test-key",
        api_key_file="",
        embedding_provider="lexical",
        context_soft_tokens=6000,
        context_hard_tokens=20000,
        daily_budget_usd=2,
        turn_budget_usd=0.2,
    )


@pytest.fixture
def db(config):
    assert config.database_url.rsplit("/", 1)[-1].endswith("_test"), (
        "Only explicitly named test databases may be reset"
    )
    admin = create_engine(config.database_url.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    name = config.database_url.rsplit("/", 1)[-1]
    with admin.connect() as conn:
        if not conn.scalar(text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": name}):
            assert name.replace("_", "").isalnum()
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    database = Database(config)
    Base.metadata.drop_all(database.engine)
    database.initialize()
    yield database
    database.close()


@pytest.fixture
def runtime(db, config):
    return Runtime(db, config)


@pytest.fixture
def fake():
    return FakeProvider()
