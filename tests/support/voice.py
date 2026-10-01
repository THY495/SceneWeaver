"""Shared voice test helpers."""

import pytest
from pydantic import SecretStr

from chatbot.api import create_app


@pytest.fixture
def app(config, db, fake):
    config.product = "roleplay"
    config.tts_enabled = True
    config.tts_api_key = SecretStr("test")
    app = create_app(config, db, fake)

    async def pcm(text, voice):
        yield b"\x00\x01" * 2400

    app.state.voice.pcm = pcm
    return app
