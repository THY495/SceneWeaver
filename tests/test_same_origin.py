from fastapi.testclient import TestClient

from chatbot.api import create_app


def test_same_origin_uses_public_port_while_cross_origin_is_rejected(db, config, fake):
    # Docker may publish the internal Studio port on a different host port.
    with TestClient(create_app(config, db, fake), base_url="http://127.0.0.1:8866") as client:
        assert client.get("/api/health", headers={"Origin": "http://127.0.0.1:8866"}).status_code == 200
        assert client.get("/api/health", headers={"Origin": "https://other.example"}).status_code == 403
