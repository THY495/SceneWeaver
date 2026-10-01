import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from chatbot.api import create_app
from chatbot.experiment_templates import template
from chatbot.observations import visible_records
from chatbot.storage import Story


def recipe():
    value = template("partial-hearing")
    for person in value["people"]:
        person["unnoticed"] = []
    value["narrative"] = {
        "entry": "private",
        "nodes": [
            {
                "id": "private",
                "title": "秘密会议",
                "text": "暗号是蓝鹭。",
                "observers": ["curator", "restorer"],
                "mode": "backstage",
                "participants": ["curator", "restorer"],
                "background": "a" * 64,
            },
            {"id": "public", "title": "雨停", "text": "雨停了。", "observers": ["player"]},
        ],
        "edges": [{"id": "exit", "source": "private", "target": "public", "label": "暗号已传递，结束会议"}],
    }
    return value


@pytest.mark.parametrize("branch", ["公开交接", "暂缓并封存"])
async def test_ui_authored_three_npc_package_runs_backstage_and_reaches_either_ending(
    db, config, fake, branch
):
    from chatbot.agents import Agents
    from chatbot.auto_routing import assess
    from chatbot.game_api import create_game_app
    from chatbot.game_backstage import schedule_backstage
    from chatbot.game_saves import GameSaves, NewGame
    from chatbot.game_scope import GameExecutionScope
    from chatbot.observations import append_record
    from chatbot.runtime import Runtime
    from chatbot.storage import Conversation, ExperimentInitial, Story

    config.product = "roleplay"
    package = Path(__file__).resolve().parents[1] / "evals/story-demo/20260926-r13-ui-three-npc-story.json"
    value = json.loads(package.read_text(encoding="utf-8"))["recipe"]
    with TestClient(create_app(config, db, fake)) as studio:
        bid = str(uuid4())
        saved = studio.put(
            "/api/role/experiments/blueprints/" + bid,
            json={"expected_version": 0, "recipe": value},
        )
        assert saved.status_code == 200, saved.text
        source = studio.post(
            "/api/role/experiments/start",
            json={"request_id": "ui-authored-source", "blueprint_id": bid, "blueprint_version": 1},
        )
        assert source.status_code == 201, source.text

        async def ready():
            pass

        studio.app.state.runtime.model_readiness.require = ready
        library = GameSaves(db, config, studio.app.state.runtime, source_story_id=source.json()["story_id"])
        save = await library.create(NewGame(request_id="ui-authored-game", label="三人门厅"))
        with db.session() as s:
            player = s.get(Conversation, save["id"])
            origin = s.get(ExperimentInitial, player.story_id)
            actor_ids = origin.receipt["actors"]
            initial = s.get(Story, player.story_id)
            assert initial.settings["narrative"]["mode"] == "backstage"
            assert initial.settings["paused"]
        runtime = Runtime(
            db, config, execution_scope=GameExecutionScope(config.workspace_owner, library.release_id)
        )
        task_id = schedule_backstage(db, runtime)
        assert task_id
        fake.replies.update(
            {
                "director": {
                    "beat_id": value["narrative"]["nodes"][0]["direction"]["beats"][0]["id"],
                    "after_plan": "yield_player",
                    "remaining_work": [],
                    "reason": "程安说明准备情况",
                    "jobs": [
                        {
                            "id": "say",
                            "actor": actor_ids["archivist"],
                            "objective": "说明档案已经准备交接",
                        }
                    ],
                },
                "director_assignment": {"checks": [], "accepted": True, "issues": []},
                "role_intent": {
                    "act": "speak",
                    "purpose": "说明档案准备情况",
                    "wait_for_player": False,
                    "source_ids": [],
                    "deception": False,
                    "retrieval": "context",
                    "remaining_request": "",
                },
                "dialogue": {
                    "text": "档案已经准备交接。",
                    "claims": [],
                    "spoken_parts": ["档案已经准备交接。"],
                },
            }
        )

        def progress(messages):
            data = json.loads(messages[1]["content"])
            event = next(e for e in data["events"] if e["event_id"] in data["current_event_ids"])
            return {
                "status": "met",
                "reason": "程安已说明",
                "evidence": [{"event_id": event["event_id"], "quote": event["text"]}],
            }

        fake.replies["director_progress"] = progress
        task = runtime.claim("ui-authored-backstage", background=True)
        assert task.id == task_id
        result = await Agents(db, config, runtime, fake).execute(task)
        assert runtime.finish(task.id, task.lease_version, result)
        assert schedule_backstage(db, runtime) == player.story_id
        with TestClient(create_game_app(config, db, fake, player_id=save["id"])) as game:
            snapshot = game.get(f"/api/role/players/{save['id']}/snapshot")
            assert snapshot.status_code == 200, snapshot.text
            frame = snapshot.json()["frame"]
            assert frame["title"] == "门厅会面"
            assert {actor["id"] for actor in snapshot.json()["characters"]} >= {
                actor_ids["archivist"],
                actor_ids["restorer"],
                actor_ids[
                    next(alias for alias in actor_ids if alias not in {"archivist", "restorer", "player"})
                ],
            }
            assert "闭馆前的协商" not in snapshot.text
            assert "档案已经准备交接。" not in snapshot.text
        with db.session() as s:
            final = s.get(Story, player.story_id)
            assert not final.settings["paused"]
            assert final.settings["narrative"]["path"] == [
                value["narrative"]["nodes"][0]["id"],
                value["narrative"]["nodes"][1]["id"],
            ]
            assert all(
                s.get(Conversation, actor_ids[alias]).current_state["location"] == "门厅"
                for alias in actor_ids
            )

        sid = player.story_id
        player_node = value["narrative"]["nodes"][1]
        with TestClient(create_game_app(config, db, fake, player_id=save["id"])) as game:
            path = f"/api/game/stories/{sid}/narrative"
            opened = game.post(
                path + "/advance",
                json={
                    "request_id": "finish-meeting-opening",
                    "node_id": player_node["id"],
                    "expected_version": 1,
                },
            )
            assert opened.status_code == 200, opened.text
            assert opened.json()["execution"]["phase"] == "interactive"

        verdict = "公开" if branch == "公开交接" else "暂缓公开并封存"
        consent_evidence = []
        with db.session.begin() as s:
            people = [s.get(Conversation, actor_ids[alias]) for alias in actor_ids]
            for alias in ("archivist", "restorer"):
                consent = f"我同意现在{verdict}交接记录。"
                record = append_record(
                    s,
                    story_id=sid,
                    kind="speech",
                    actor=s.get(Conversation, actor_ids[alias]),
                    text=consent,
                    observer_ids=[person.id for person in people],
                    dedupe=f"ui-branch-consent:{alias}",
                )
                consent_evidence.append({"event_id": record.id, "quote": consent})
        player_text = f"我决定现在{verdict}交接记录。"
        runtime.submit(save["id"], config.workspace_owner, player_text, "ui-branch-choice")
        response_task = runtime.claim("ui-branch-route")
        assert response_task.kind == "respond"
        selected = next(edge for edge in value["narrative"]["edges"] if edge["label"] == branch)
        other = next(
            edge
            for edge in value["narrative"]["edges"]
            if edge["source"] == player_node["id"] and edge != selected
        )
        fake.replies["scene_routing"] = {
            "assessments": [
                {
                    "edge_id": selected["id"],
                    "status": "met",
                    "reason": "玩家与双方明确同意",
                    "evidence": [
                        *consent_evidence,
                        {"event_id": response_task.payload["event_id"], "quote": player_text},
                    ],
                },
                {"edge_id": other["id"], "status": "not_met", "reason": "意愿方向相反", "evidence": []},
            ]
        }
        await assess(db, fake, response_task)
        assert runtime.finish(response_task.id, response_task.lease_version, {})
        with TestClient(create_game_app(config, db, fake, player_id=save["id"])) as game:
            snapshot_path = f"/api/role/players/{save['id']}/snapshot"
            before = game.get(snapshot_path).json()
            assert before["frame"]["auto_ready"]
            jumped = game.post(path + "/auto-continue", json=before["frame"]["auto_ready"])
            assert jumped.status_code == 200, jumped.text
            target = next(node for node in value["narrative"]["nodes"] if node["id"] == selected["target"])
            assert jumped.json()["title"] == target["title"]
            assert jumped.json()["ending"] and not jumped.json()["finished_at"]
            closed = game.post(
                path + "/advance",
                json={"request_id": "finish-ui-branch", "node_id": target["id"], "expected_version": 1},
            )
            assert closed.status_code == 200, closed.text
            assert closed.json()["finished_at"]
            finale = game.get(snapshot_path).json()
            assert finale["frame"]["title"] == target["title"]
            assert not finale["interaction"]["allowed"]


def test_backstage_is_private_paused_and_author_can_advance(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as c:
        base = "/api/role/experiments"
        bid = str(uuid4())
        saved = c.put(base + "/blueprints/" + bid, json={"expected_version": 0, "recipe": recipe()})
        assert saved.status_code == 200, saved.text
        result = c.post(
            base + "/start", json={"request_id": "backstage", "blueprint_id": bid, "blueprint_version": 1}
        )
        assert result.status_code in (200, 201), result.text
        run = result.json()
        sid = run["story_id"]
        path = base + f"/stories/{sid}/narrative"
        player = c.get(path).json()
        assert player["mode"] == "backstage" and player["choices"] == []
        assert c.put(f"/api/role/stories/{sid}/settings", json={"paused": False}).status_code == 409
        assert all(secret not in str(player) for secret in ["蓝鹭", "秘密会议", "exit", "a" * 64, "private"])
        author = c.get(path + "/director").json()
        assert author["text"] == "暗号是蓝鹭。" and author["choices"][0]["id"] == "exit"
        with db.session() as s:
            assert s.get(Story, sid).settings["paused"]
            assert "蓝鹭" not in str([r.text for r in visible_records(s, run["player_id"])])
            assert "蓝鹭" in str([r.text for r in visible_records(s, run["actors"]["curator"])])
        body = {"request_id": "exit", "edge_id": "exit", "expected_revision": 1}
        assert c.post(path + "/choose", json=body).status_code == 409
        advanced = c.post(path + "/director/choose", json=body)
        assert advanced.status_code == 200, advanced.text
        assert advanced.json()["title"] == "雨停"
        assert c.post(path + "/director/choose", json=body).json() == advanced.json()
        assert c.get(path).json()["text"] == "雨停了。"
        with db.session() as s:
            assert "蓝鹭" not in str([r.text for r in visible_records(s, run["player_id"])])


def test_backstage_rejects_player_at_location(db, config, fake):
    config.product = "roleplay"
    value = recipe()
    value["player"]["location"] = value["people"][0]["location"]
    with TestClient(create_app(config, db, fake)) as c:
        bid = str(uuid4())
        base = "/api/role/experiments"
        assert (
            c.put(base + "/blueprints/" + bid, json={"expected_version": 0, "recipe": value}).status_code
            == 200
        )
        result = c.post(
            base + "/start",
            json={"request_id": "invalid-backstage", "blueprint_id": bid, "blueprint_version": 1},
        )
        assert result.status_code == 409, result.text


def test_backstage_entry_move_is_applied_before_colocation_and_release_check(db, config, fake):
    from chatbot.backstage_release import release_plan
    from chatbot.storage import Conversation

    config.product = "roleplay"
    value = recipe()
    value["people"][0]["location"] = "门厅"
    value["narrative"]["nodes"][0]["entry_locations"] = [{"actor": "curator", "location": "阅览室"}]
    assert release_plan(value["narrative"], value["people"], value["player"])["hops"] == [
        ("private", "exit", "public")
    ]
    with TestClient(create_app(config, db, fake)) as c:
        bid = str(uuid4())
        base = "/api/role/experiments"
        saved = c.put(base + "/blueprints/" + bid, json={"expected_version": 0, "recipe": value})
        assert saved.status_code == 200, saved.text
        result = c.post(
            base + "/start",
            json={"request_id": "entry-arrival", "blueprint_id": bid, "blueprint_version": 1},
        )
        assert result.status_code == 201, result.text
        run = result.json()
        with db.session() as s:
            curator = s.get(Conversation, run["actors"]["curator"])
            assert curator.current_state["location"] == "阅览室"
            assert any(r.kind == "scene_change" for r in visible_records(s, run["player_id"]))
            assert "蓝鹭" not in str([r.text for r in visible_records(s, run["player_id"])])


async def test_backstage_npc_step_commits_without_player_observation(db, config, runtime, fake):
    from support.story import intention

    from chatbot.agents import Agents

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as c:
        bid = str(uuid4())
        base = "/api/role/experiments"
        c.put(base + "/blueprints/" + bid, json={"expected_version": 0, "recipe": recipe()})
        run = c.post(
            base + "/start",
            json={"request_id": "backstage-step", "blueprint_id": bid, "blueprint_version": 1},
        ).json()
        sid = run["story_id"]
        steps = f"/api/role/stories/{sid}/steps"
        assert (
            c.post(steps, json={"request_id": "outsider", "actor_id": run["actors"]["reporter"]}).status_code
            == 409
        )
        r = c.post(steps, json={"request_id": "one", "actor_id": run["actors"]["curator"]})
        assert r.status_code == 201, r.text
        tid = r.json()["task_id"]
        fake.replies["role_intent"] = intention(run["actors"]["restorer"], wait_for_player=False)
        fake.replies["dialogue"] = "我们先核对一下。"
        c.post(steps + f"/{tid}/presence", json={"visible": True})
        task = runtime.claim("backstage", background=True)
        result = await Agents(db, config, runtime, fake).execute(task)
        runtime.finish(task.id, task.lease_version, result)
        state = c.get(steps + f"/{tid}").json()
        assert state["status"] == "completed", state
        assert len(state["events"]) == 1
        with db.session() as s:
            assert state["events"][0]["id"] not in {r.id for r in visible_records(s, run["player_id"])}
            assert state["events"][0]["id"] in {r.id for r in visible_records(s, run["actors"]["restorer"])}


def test_independent_game_auto_commits_fixed_backstage_prelude_without_revealing_it(db, config, fake):
    from chatbot.game_api import create_game_app
    from chatbot.game_saves import GameSaves, NewGame
    from chatbot.storage import Conversation, ExperimentInitial, StoryEvent

    config.product = "roleplay"
    value = recipe()
    value["narrative"]["nodes"][0]["opening"] = [
        {"id": "secret-line", "kind": "dialogue", "speaker": "curator", "text": "暗号是蓝鹭。"}
    ]
    with TestClient(create_app(config, db, fake)) as client:
        bid = str(uuid4())
        assert (
            client.put(
                "/api/role/experiments/blueprints/" + bid,
                json={"expected_version": 0, "recipe": value},
            ).status_code
            == 200
        )
        source = client.post(
            "/api/role/experiments/start",
            json={"request_id": "scripted-source", "blueprint_id": bid, "blueprint_version": 1},
        ).json()

        async def ready():
            pass

        client.app.state.runtime.model_readiness.require = ready
        library = GameSaves(db, config, client.app.state.runtime, source_story_id=source["story_id"])
        saved = asyncio.run(library.create(NewGame(request_id="play", label="试玩")))
        assert not fake.calls
        with TestClient(create_game_app(config, db, fake, player_id=saved["id"])) as game:
            snapshot = game.get(f"/api/role/players/{saved['id']}/snapshot")
            assert snapshot.status_code == 200, snapshot.text
            assert snapshot.json()["frame"]["title"] == "雨停"
            assert "蓝鹭" not in snapshot.text
            assert "秘密会议" not in snapshot.text
        with db.session() as s:
            player = s.get(Conversation, saved["id"])
            story = s.get(Story, player.story_id)
            origin = s.get(ExperimentInitial, story.id)
            assert story.settings["narrative"]["node"] == "public"
            assert story.settings["narrative"]["path"] == ["private", "public"]
            assert not story.settings["paused"]
            transition = (
                s.query(StoryEvent).filter_by(story_id=story.id, request_id="game-scripted:private").one()
            )
            assert transition.payload["target"] == "public"
            assert "蓝鹭" not in str([row.text for row in visible_records(s, player.id)])
            curator = origin.receipt["actors"]["curator"]
            assert "蓝鹭" in str([row.text for row in visible_records(s, curator)])
            assert "蓝鹭" not in str(
                [row.text for row in visible_records(s, origin.receipt["actors"]["reporter"])]
            )


def test_game_release_rejects_dynamic_or_nonprefix_backstage():
    from chatbot.backstage_release import scripted_prefix

    value = recipe()
    graph = value["narrative"]
    assert len(scripted_prefix(graph, value["people"], value["player"])) == 1
    graph["nodes"][0]["direction"] = {
        "objective": "自由协商",
        "beats": [{"id": "talk", "participants": ["curator"], "completion": "协商完毕"}],
    }
    with pytest.raises(ValueError, match="固定对白"):
        scripted_prefix(graph, value["people"], value["player"])
    graph["nodes"][0].pop("direction")
    graph["entry"] = "public"
    graph["edges"] = [
        {"id": "enter", "source": "public", "target": "private", "label": "幕后"},
        *graph["edges"],
    ]
    with pytest.raises(ValueError, match="入口连续"):
        scripted_prefix(graph, value["people"], value["player"])


async def test_game_directed_backstage_runs_one_grounded_step_then_enters_player_scene(db, config, fake):
    from chatbot.agents import Agents
    from chatbot.game_api import create_game_app
    from chatbot.game_backstage import MAX_STEPS, schedule_backstage
    from chatbot.game_saves import GameSaves, NewGame
    from chatbot.game_scope import GameExecutionScope
    from chatbot.runtime import Runtime
    from chatbot.storage import Conversation, ExperimentInitial, Story, Task

    config.product = "roleplay"
    value = recipe()
    value["narrative"]["nodes"][0]["direction"] = {
        "objective": "商议交接暗号",
        "beats": [
            {
                "id": "agree",
                "title": "确认交接",
                "objective": "一人说明准备",
                "completion": "馆长明确说已经准备交接",
                "participants": ["curator"],
            }
        ],
    }
    value["narrative"]["nodes"][1]["entry_locations"] = [
        {"actor": alias, "location": value["player"]["location"]} for alias in ("curator", "restorer")
    ]
    with TestClient(create_app(config, db, fake)) as studio:
        bid = str(uuid4())
        saved = studio.put(
            "/api/role/experiments/blueprints/" + bid,
            json={"expected_version": 0, "recipe": value},
        )
        assert saved.status_code == 200, saved.text
        source = studio.post(
            "/api/role/experiments/start",
            json={"request_id": "directed-source", "blueprint_id": bid, "blueprint_version": 1},
        ).json()

        async def ready():
            pass

        studio.app.state.runtime.model_readiness.require = ready
        library = GameSaves(db, config, studio.app.state.runtime, source_story_id=source["story_id"])
        save = await library.create(NewGame(request_id="directed-play", label="自动幕后"))
        with db.session() as s:
            player = s.get(Conversation, save["id"])
            sid = player.story_id
            origin = s.get(ExperimentInitial, sid)
            assert s.get(Story, sid).settings["narrative"]["mode"] == "backstage"
        runtime = Runtime(
            db, config, execution_scope=GameExecutionScope(config.workspace_owner, library.release_id)
        )
        task_id = schedule_backstage(db, runtime)
        assert task_id
        assert schedule_backstage(db, runtime) is None
        curator = origin.receipt["actors"]["curator"]
        with db.session.begin() as s:
            from chatbot.experiments import valid_step

            npc = s.get(Conversation, curator)
            original = npc.current_state
            npc.current_state = {**original, "location": "elsewhere"}
            assert not valid_step(s, s.get(Task, task_id), s.get(Story, sid))
            npc.current_state = original
        task = runtime.claim("game-backstage-test", background=True)
        assert task.id == task_id and task.kind == "npc_step"
        fake.replies.update(
            {
                "director": {
                    "beat_id": "agree",
                    "after_plan": "yield_player",
                    "remaining_work": [],
                    "reason": "馆长发言",
                    "jobs": [{"id": "say", "actor": curator, "objective": "说明准备交接"}],
                },
                "director_assignment": {"checks": [], "accepted": True, "issues": []},
                "role_intent": {
                    "act": "speak",
                    "purpose": "说明准备",
                    "wait_for_player": False,
                    "source_ids": [],
                    "deception": False,
                    "retrieval": "context",
                    "remaining_request": "",
                },
                "dialogue": {
                    "text": "我已经准备交接。",
                    "claims": [],
                    "spoken_parts": ["我已经准备交接。"],
                },
            }
        )

        def progress(messages):
            import json

            data = json.loads(messages[1]["content"])
            event = next(e for e in data["events"] if e["event_id"] in data["current_event_ids"])
            return {
                "status": "met",
                "reason": "馆长已说明",
                "evidence": [{"event_id": event["event_id"], "quote": event["text"]}],
            }

        fake.replies["director_progress"] = progress
        result = await Agents(db, config, runtime, fake).execute(task)
        assert runtime.finish(task.id, task.lease_version, result)
        assert schedule_backstage(db, runtime) == sid
        assert schedule_backstage(db, runtime) is None
        with TestClient(create_game_app(config, db, fake, player_id=save["id"])) as game:
            snapshot = game.get(f"/api/role/players/{save['id']}/snapshot")
            assert snapshot.status_code == 200, snapshot.text
            assert snapshot.json()["frame"]["title"] == "雨停"
            assert {c["id"] for c in snapshot.json()["characters"]} >= {
                origin.receipt["actors"]["curator"],
                origin.receipt["actors"]["restorer"],
            }
            assert "蓝鹭" not in snapshot.text
            assert "我已经准备交接" not in snapshot.text
        with db.session() as s:
            assert s.get(Story, sid).settings["narrative"]["path"] == ["private", "public"]
            assert "我已经准备交接" in str([r.text for r in visible_records(s, curator)])
            assert "蓝鹭" not in str([r.text for r in visible_records(s, save["id"])])
            assert any(r.kind == "scene_change" for r in visible_records(s, save["id"]))
            assert all(
                s.get(Conversation, origin.receipt["actors"][alias]).current_state["location"]
                == value["player"]["location"]
                for alias in ("curator", "restorer")
            )
            assert s.get(Task, task_id).status == "completed"

        interrupted = await library.create(NewGame(request_id="directed-retry", label="恢复幕后"))
        failed_id = schedule_backstage(db, runtime)
        failed = runtime.claim("game-backstage-failed", background=True)
        assert failed.id == failed_id
        assert runtime.finish(failed.id, failed.lease_version, error="synthetic provider failure")
        assert schedule_backstage(db, runtime) is None
        with TestClient(create_game_app(config, db, fake, player_id=interrupted["id"])) as game:
            path = f"/api/role/players/{interrupted['id']}/snapshot"
            frame = game.get(path).json()["frame"]
            assert frame["auto_status"] == "error"
            assert "synthetic provider failure" not in str(frame)
            assert game.post("/api/game/backstage/retry").json() == {"status": "running"}
            assert game.post("/api/game/backstage/retry").json() == {"status": "running"}
        resumed_id = schedule_backstage(db, runtime)
        assert resumed_id and resumed_id != failed_id
        for _ in range(1, MAX_STEPS):
            waiting = runtime.claim("game-backstage-bounded", background=True)
            assert waiting is not None
            assert runtime.finish(waiting.id, waiting.lease_version, {"experiment": {"outcome": "wait"}})
            schedule_backstage(db, runtime)
        with TestClient(create_game_app(config, db, fake, player_id=interrupted["id"])) as game:
            assert (
                game.get(f"/api/role/players/{interrupted['id']}/snapshot").json()["frame"]["auto_status"]
                == "stalled"
            )
            assert game.post("/api/game/backstage/retry").status_code == 409
        assert schedule_backstage(db, runtime) is None
