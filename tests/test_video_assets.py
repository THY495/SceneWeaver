import hashlib
import io
import struct

import pytest
from fastapi import APIRouter, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from chatbot.video_assets import install_video_routes, store_video, validate_mp4, video_dir


def box(kind, data):
    return struct.pack(">I4s", len(data) + 8, kind) + data


def container():
    # Structural fixture, deliberately not a decodable movie.
    return box(b"ftyp", b"isom\x00\x00\x00\x00mp42") + box(b"moov", b"track") + box(b"mdat", b"frames")


def test_video_upload_owner_scope_range_and_immutable_dedup(config):
    app = FastAPI()
    router = APIRouter(prefix="/api/role/presentation")

    def who(x_owner: str = Header(default="author-one")):
        return x_owner

    install_video_routes(router, config, who)
    app.include_router(router)
    data = container()
    key = hashlib.sha256(data).hexdigest()
    with TestClient(app) as client:
        result = client.post("/api/role/presentation/videos", files={"file": ("../movie.mp4", data)})
        assert result.status_code == 201, result.text
        assert result.json() == {
            "id": key,
            "bytes": len(data),
            "media_type": "video/mp4",
            "codec_verified": False,
        }
        path = video_dir(config, "author-one") / (key + ".mp4")
        mtime = path.stat().st_mtime_ns
        assert (
            client.post("/api/role/presentation/videos", files={"file": ("again.mp4", data)}).status_code
            == 201
        )
        assert path.stat().st_mtime_ns == mtime
        url = "/api/role/presentation/videos/" + key
        partial = client.get(url, headers={"Range": "bytes=8-19"})
        assert partial.status_code == 206
        assert partial.content == data[8:20]
        assert partial.headers["content-range"] == f"bytes 8-19/{len(data)}"
        assert partial.headers["content-type"] == "video/mp4"
        assert client.get(url, headers={"x-owner": "author-two"}).status_code == 404
        assert client.get(url, headers={"Range": "bytes=999-1000"}).status_code == 416
        assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not a video",
        container()[:-1],
        box(b"ftyp", b"isom0000"),
        box(b"ftyp", b"qt  0000") + box(b"moov", b"x") + box(b"mdat", b"x"),
        container() + struct.pack(">I4s", 1, b"free"),
    ],
)
def test_bad_container_rejected(tmp_path, data):
    path = tmp_path / "input.mp4"
    path.write_bytes(data)
    with pytest.raises(HTTPException) as error:
        validate_mp4(path)
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_failed_upload_does_not_publish_partial_file(config, monkeypatch):
    import chatbot.video_assets as module

    monkeypatch.setattr(module, "MAX_VIDEO", 16)
    with pytest.raises(HTTPException) as error:
        await store_video(UploadFile(io.BytesIO(container()), filename="video.mp4"), config, "owner")
    assert error.value.status_code == 413
    assert not list(video_dir(config, "owner").iterdir())


def test_studio_video_routes_are_not_exposed_in_game(db, config, fake):
    from chatbot.api import create_app
    from chatbot.game_api import create_game_app

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake, serve_web=False)) as studio:
        upload = studio.post("/api/role/presentation/videos", files={"file": ("scene.mp4", container())})
        assert upload.status_code == 201, upload.text
        key = upload.json()["id"]
        assert studio.get("/api/role/presentation/videos/" + key).status_code == 200
        ticket = studio.post(f"/api/role/presentation/videos/{key}/ticket").json()["ticket"]
        preview = f"/api/role/presentation/video-preview/{key}"
        assert studio.get(preview).status_code == 401
        assert studio.get(preview, params={"ticket": ticket}).content == container()
    with TestClient(create_game_app(config, db, fake, player_id="bound")) as game:
        assert game.get("/api/role/presentation/videos/" + key).status_code == 404
        assert game.get(preview, params={"ticket": ticket}).status_code == 404
        assert (
            game.post("/api/role/presentation/videos", files={"file": ("scene.mp4", container())}).status_code
            == 404
        )
