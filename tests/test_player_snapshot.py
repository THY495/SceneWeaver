from fastapi.testclient import TestClient
from sqlalchemy import select

from chatbot.api import create_app
from chatbot.observations import append_record
from chatbot.storage import Conversation, RoleMemory, Story


def test_snapshot_scopes_partial_hearing_and_rejects_npc(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run = client.post(
            "/api/role/experiments/start", json={"request_id": "snapshot", "template_id": "partial-hearing"}
        ).json()
        with db.session.begin() as s:
            npc = s.get(Conversation, run["actors"]["reporter"])
            event = append_record(
                s,
                story_id=run["story_id"],
                kind="speech",
                actor=npc,
                text="你好，秘密蓝鹭。",
                observer_ids=[npc.id, run["player_id"]],
                dedupe="snapshot-secret",
                perceptions={run["player_id"]: {"text": "你好", "speaker_known": False}},
            )
            event_id = event.id
        url = f"/api/role/players/{run['player_id']}/snapshot"
        response = client.get(url)
        assert response.status_code == 200, response.text
        value = response.json()
        assert [p["name"] for p in value["characters"]] == ["许遥"]
        assert "蓝鹭" not in response.text
        assert next(e for e in value["events"] if e["id"] == event_id)["actor_id"] is None
        assert not value["inventory"]["enabled"]
        assert not value["interaction"]["allowed"]
        assert client.get(f"/api/role/players/{npc.id}/snapshot").status_code == 404
        assert client.get("/api/role/players/missing/snapshot").status_code == 404
        with db.session.begin() as s:
            s.get(Conversation, run["player_id"]).owner_id = "different-owner"
        assert client.get(url).status_code == 404


def test_offscreen_scene_work_never_blocks_player_input(db, config, fake, runtime):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        started = client.post(
            "/api/role/experiments/start",
            json={"request_id": "offscreen-not-player-busy", "template_id": "partial-hearing"},
        )
        assert started.status_code == 201, started.text
        run = started.json()
        with db.session.begin() as s:
            story = s.get(Story, run["story_id"])
            story.settings = {**story.settings, "paused": False}
            runtime.enqueue(
                s,
                "scene_run",
                config.workspace_owner,
                {"story_id": story.id, "scene": "另一个房间", "audience": []},
            )
        url = f"/api/role/players/{run['player_id']}/snapshot"
        offscreen = client.get(url)
        assert offscreen.status_code == 200, offscreen.text
        assert offscreen.json()["interaction"] == {"allowed": True, "reason": "", "responding": False}
        with db.session.begin() as s:
            runtime.enqueue(
                s,
                "respond",
                config.workspace_owner,
                {"text": "玩家实际发言"},
                session_id=run["player_id"],
            )
        player = client.get(url).json()["interaction"]
        assert player["responding"] and not player["allowed"]


def test_snapshot_uses_one_database_view_under_concurrent_commit(db, config, fake, monkeypatch):
    import chatbot.player_snapshot as module

    config.product = "roleplay"
    original = module.player_snapshot
    inserted = []

    def concurrent(s, player, story, settings, anchor=None, **kwargs):
        if not inserted:
            with db.session.begin() as other:
                changed = other.get(Story, story.id)
                changed.settings = {**changed.settings, "paused": False}
                npc = other.get(Conversation, player.id)
                e = append_record(
                    other,
                    story_id=story.id,
                    kind="speech",
                    actor=npc,
                    text="并发提交之后的对白",
                    observer_ids=[player.id],
                    dedupe="concurrent-snapshot",
                )
                inserted.append(e.id)
        return original(s, player, story, settings, anchor)

    monkeypatch.setattr(module, "player_snapshot", concurrent)
    with TestClient(create_app(config, db, fake)) as client:
        run = client.post(
            "/api/role/experiments/start", json={"request_id": "consistent", "template_id": "partial-hearing"}
        ).json()
        url = f"/api/role/players/{run['player_id']}/snapshot"
        first = client.get(url).json()
        assert first["settings"]["paused"]
        assert inserted[0] not in [e["id"] for e in first["events"]]
        second = client.get(url).json()
        assert not second["settings"]["paused"]
        assert inserted[0] in [e["id"] for e in second["events"]]


def test_snapshot_long_history_anchor_and_read_only_inventory(db, config, fake):
    from test_item_transfers import setup

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run, path, _ = setup(client)
        initial_inventory = client.get(path).json()
        with db.session.begin() as s:
            player = s.get(Conversation, run["player_id"])
            ids = []
            for index in range(115):
                e = append_record(
                    s,
                    story_id=run["story_id"],
                    kind="speech",
                    actor=player,
                    text=f"历史段落{index}",
                    observer_ids=[player.id],
                    dedupe=f"history:{index}",
                )
                ids.append(e.id)
        url = f"/api/role/players/{run['player_id']}/snapshot"
        latest = client.get(url).json()
        assert len(latest["events"]) == 100
        assert ids[0] not in [e["id"] for e in latest["events"]]
        anchored = client.get(url + "?anchor=" + ids[0]).json()
        assert ids[0] in [e["id"] for e in anchored["events"]]
        assert ids[-1] in [e["id"] for e in anchored["events"]]
        assert anchored["inventory"] == initial_inventory
        assert anchored["interaction"]["responding"]
        assert not anchored["interaction"]["allowed"]
        assert client.get(path).json() == initial_inventory
        assert len(client.get(url + "?anchor=" + ids[-1]).json()["events"]) == 100


def test_snapshot_over_limit_never_skips_bookmark_or_leaks_private_exchange(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run = client.post(
            "/api/role/experiments/start",
            json={"request_id": "long-snapshot", "template_id": "partial-hearing"},
        ).json()
        with db.session.begin() as s:
            player = s.get(Conversation, run["player_id"])
            reporter = s.get(Conversation, run["actors"]["reporter"])
            curator = s.get(Conversation, run["actors"]["curator"])
            private = append_record(
                s,
                story_id=run["story_id"],
                actor=curator,
                kind="speech",
                text="仅两人知道的蓝鹭密码",
                observer_ids=[curator.id, reporter.id],
                dedupe="private-long",
            )
            private_id = private.id
            ids = []
            for index in range(2105):
                event = append_record(
                    s,
                    story_id=run["story_id"],
                    actor=reporter,
                    kind="speech",
                    text=f"长历史第{index}段",
                    observer_ids=[reporter.id, player.id],
                    dedupe=f"long:{index}",
                )
                ids.append(event.id)
        url = f"/api/role/players/{run['player_id']}/snapshot"
        first = client.get(url + "?anchor=" + ids[0]).json()
        assert len(first["events"]) == 2100
        assert first["events"][0]["id"] == ids[0]
        assert first["events"][-1]["id"] == ids[2099]
        assert first["history_more_after"]
        second = client.get(url + "?anchor=" + first["history_next_anchor"]).json()
        visible = [event["id"] for event in second["events"]]
        assert ids[2099] in visible and ids[-1] in visible
        assert not second["history_more_after"]
        assert "蓝鹭密码" not in str(first) + str(second)
        unknown = client.get(url + "?anchor=" + private_id).json()
        assert "蓝鹭密码" not in str(unknown)
        assert private_id not in [event["id"] for event in unknown["events"]]
        with db.session() as s:
            memories = list(s.scalars(select(RoleMemory).where(RoleMemory.story_id == run["story_id"])))
            assert {m.actor_id for m in memories if private_id in m.source_ids} == {curator.id, reporter.id}
        # Moving into the other room changes the cast, not knowledge of old private speech.
        with db.session.begin() as s:
            player = s.get(Conversation, run["player_id"])
            player.current_state = {**player.current_state, "location": curator.current_state["location"]}
        moved = client.get(url).json()
        assert curator.id in [c["id"] for c in moved["characters"]]
        assert "蓝鹭密码" not in str(moved)
