import importlib.util
import json
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from support.story import launch

from chatbot.api import create_app
from chatbot.event_projection import projected_exchanges
from chatbot.observations import append_record, observed_record, record_data, visible_records
from chatbot.role_memory import RoleMemoryService
from chatbot.storage import Conversation, Observation, RoleMemory, SceneRecord
from chatbot.voice import chunks


def test_authored_partial_unknown_speech_is_frozen_and_does_not_leak(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        body = {
            "request_id": "partial",
            "actor_id": actors[0],
            "text": "门口见。密令是紫色鹦鹉。",
            "observations": [
                {"actor_id": actors[1], "text": "门口见。", "speaker_known": False},
                {"actor_id": player},
            ],
        }
        url = f"/api/role/stories/{story}/speech-events"
        result = client.post(url, json=body)
        assert result.status_code == 201, result.text
        eid = result.json()["id"]
        assert client.post(url, json=body).json()["id"] == eid
        assert client.post(url, json={**body, "text": "不同原文。"}).status_code == 409
        timeline = client.get(f"/api/role/stories/{story}/timeline", params={"actor_id": actors[1]}).json()
        row = next(e for e in timeline["events"] if e["id"] == eid)
        assert row["text"] == "门口见。" and row["actor_id"] is None
        assert row["speaker"] == "未辨认的声音" and "observers" not in row
        assert "紫色鹦鹉" not in json.dumps(timeline, ensure_ascii=False)
        with db.session() as s:
            assert not observed_record(s, actors[2], eid)
            # Accessing another view in the same ORM session never overwrites canonical text.
            assert observed_record(s, actors[1], eid).text == "门口见。"
            assert observed_record(s, player, eid).text == body["text"]
            assert s.get(SceneRecord, eid).text == body["text"]
            episode = s.scalar(
                select(RoleMemory).where(
                    RoleMemory.actor_id == actors[1], RoleMemory.source_ids.contains([eid])
                )
            )
            assert "紫色鹦鹉" not in episode.content and "未辨认的声音" in episode.content
        assert (
            client.patch(f"/api/role/actors/{actors[0]}/location", json={"location": "楼上"}).status_code
            == 200
        )
        task = runtime.submit(player, config.workspace_owner, "你听到了什么？", "ask-hearing")
        context, info = RoleMemoryService(db, config, runtime, fake).context(task, actors[1])
        assert "紫色鹦鹉" not in json.dumps(context, ensure_ascii=False)
        assert "门口见。" in json.dumps(context, ensure_ascii=False)
        from chatbot.role_policy import review_grounding

        grounding = review_grounding(info, [], {"claims": [{"source_id": eid}]}, {eid: "门口见。"})
        ref = grounding["referenced_records_with_provenance"][0]
        assert ref["utterance_observation"]["acquisition"] == "observed_utterance"
        assert ref["utterance_observation"]["speaker_known"] is False
        assert ref["utterance_observation"]["actor_id"] is None
        assert ref["utterance_observation"]["complete"] is False
        assert "紫色鹦鹉" not in json.dumps(grounding, ensure_ascii=False)
        assert client.delete(f"/api/role/stories/{story}/events/{eid}").status_code == 200
        with db.session() as s:
            assert not observed_record(s, actors[1], eid)
            assert s.get(Observation, (actors[1], eid)).perception == {}


def test_private_channel_and_verbatim_excerpt_are_enforced(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        body = {
            "request_id": "private",
            "actor_id": actors[0],
            "text": "仅限私下。",
            "channel": "private",
            "addressees": [actors[1]],
            "observations": [{"actor_id": player}],
        }
        url = f"/api/role/stories/{story}/speech-events"
        assert client.post(url, json=body).status_code == 400
        body["observations"] = [{"actor_id": actors[1], "text": "不存在的内容"}]
        assert client.post(url, json=body).status_code == 400
        body["observations"] = [{"actor_id": actors[1]}]
        assert client.post(url, json=body).status_code == 201
        with db.session() as s:
            assert all("仅限私下" not in r.text for r in visible_records(s, player))


def test_authored_question_creates_only_a_fully_heard_assigned_reply(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, _ = launch(client)
        url = f"/api/role/stories/{story}/speech-events"
        body = {
            "request_id": "assigned-question",
            "actor_id": actors[0],
            "text": "顾宁，你是否同意先核对交接记录？",
            "observations": [{"actor_id": actors[1]}],
            "expects_reply": True,
            "target_id": actors[1],
        }
        result = client.post(url, json=body)
        assert result.status_code == 201, result.text
        with db.session() as s:
            request = projected_exchanges(s, actors[1])
            assert len(request) == 1
            assert request[0]["question_id"] == result.json()["id"]
            assert request[0]["asker_id"] == actors[0]
            assert request[0]["target_id"] == actors[1]
            assert request[0]["status"] == "open"

        assert (
            client.post(
                url,
                json={
                    **body,
                    "request_id": "partial-question",
                    "observations": [
                        {
                            "actor_id": actors[1],
                            "text": "顾宁",
                            "speaker_known": True,
                        }
                    ],
                },
            ).status_code
            == 400
        )
        assert (
            client.post(
                url,
                json={
                    **body,
                    "request_id": "unknown-question",
                    "observations": [{"actor_id": actors[1], "speaker_known": False}],
                },
            ).status_code
            == 400
        )


def test_authored_reply_obeys_assignment_perception_lifecycle_and_idempotency(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        question = client.post(
            f"/api/role/stories/{story}/speech-events",
            json={
                "request_id": "controlled-question",
                "actor_id": actors[0],
                "text": "你是否同意先核对交接记录？",
                "observations": [{"actor_id": actors[1]}, {"actor_id": actors[2]}],
                "expects_reply": True,
                "target_id": actors[1],
            },
        )
        assert question.status_code == 201, question.text
        question_id = question.json()["id"]
        endpoint = f"/api/role/stories/{story}/reply-events"
        defer = {
            "request_id": "controlled-defer",
            "question_id": question_id,
            "actor_id": actors[1],
            "response_kind": "defer",
            "text": "我需要再核对一下，稍后回答。",
            "channel": "private",
            "addressees": [actors[0], actors[2]],
            "observations": [
                {"actor_id": actors[0]},
                {"actor_id": actors[2], "text": "稍后回答", "speaker_known": True},
            ],
        }
        wrong_actor = client.post(endpoint, json={**defer, "actor_id": actors[2]})
        assert wrong_actor.status_code == 404
        result = client.post(endpoint, json=defer)
        assert result.status_code == 201, result.text
        assert client.post(endpoint, json=defer).json() == result.json()
        assert client.post(endpoint, json={**defer, "text": "改写内容"}).status_code == 409

        frame = client.get(f"/api/role/stories/{story}/observation-frame").json()
        statuses = {
            view["actor_id"]: view["status"] for view in frame["exchange_matrix"][0]["views"]
        }
        assert statuses == {
            actors[0]: "defer",
            actors[1]: "defer",
            actors[2]: "open",
            player: "unseen",
        }
        witness = client.get(
            f"/api/role/stories/{story}/timeline", params={"actor_id": actors[2]}
        ).json()
        deferred_event = next(row for row in witness["events"] if row["id"] == result.json()["id"])
        assert deferred_event["text"] == "稍后回答"
        assert deferred_event["detail"] == {}

        answer = {
            "request_id": "controlled-answer",
            "question_id": question_id,
            "actor_id": actors[1],
            "response_kind": "answer",
            "text": "记录已经核对，我同意交接。",
            "observations": [{"actor_id": actors[0]}, {"actor_id": actors[2]}],
        }
        answered = client.post(endpoint, json=answer)
        assert answered.status_code == 201, answered.text
        frame = client.get(f"/api/role/stories/{story}/observation-frame").json()
        statuses = {
            view["actor_id"]: view["status"] for view in frame["exchange_matrix"][0]["views"]
        }
        assert statuses == {
            actors[0]: "answer",
            actors[1]: "answer",
            actors[2]: "answer",
            player: "unseen",
        }
        assert client.post(endpoint, json={**answer, "request_id": "late-refusal"}).status_code == 409
        assert fake.calls == []


def test_partial_request_metadata_and_audio_are_scoped(db, config, runtime, fake):
    config.product = "roleplay"
    config.tts_enabled = True
    config.tts_api_key = SecretStr("test")
    app = create_app(config, db, fake)
    with TestClient(app) as client:
        story, actors, player = launch(client)
        runtime.submit(player, config.workspace_owner, "听一下", "voice-partial")
        task = runtime.claim("voice-test")
        with db.session.begin() as s:
            a = s.get(Conversation, actors[0])
            event = append_record(
                s,
                story_id=story,
                actor=a,
                kind="speech",
                text="明早见。暗号不要泄露。",
                observer_ids=[a.id, actors[1], player],
                dedupe="scoped-audio",
                task_id=task.id,
                perceptions={player: {"text": "明早见。", "speaker_known": False}},
                payload={
                    "spoken_parts": ["明早见。暗号不要泄露。"],
                    "exchange": {"expects_reply": True, "target_id": actors[1]},
                },
            )
            eid = event.id
            assert projected_exchanges(s, player) == []
            assert len(projected_exchanges(s, a.id)) == 1
            view = observed_record(s, player, eid)
            assert chunks(view) == ["明早见。"]
            assert "exchange" not in record_data(view)
        data = client.get(f"/api/voice/sessions/{player}/utterances").json()
        utterance = next(r for r in data["utterances"] if r["id"] == eid)
        assert utterance["actor_id"] is None and utterance["chunks"] == ["明早见。"]
        assert "暗号" not in json.dumps(data, ensure_ascii=False)
        result = client.post(
            f"/api/voice/sessions/{player}/playbacks",
            json={"request_id": "partial-audio", "event_id": eid, "chunk_index": 0, "epoch": task.epoch},
        )
        assert result.status_code == 200, result.text
        with db.session() as s:
            player_row = s.get(Conversation, player)
            assert app.state.voice.event(s, player_row, eid).text == "明早见。"


def test_perception_migration_preserves_legacy_observations(db):
    spec = importlib.util.spec_from_file_location(
        "migration008", Path("migrations/versions/008_perception.py")
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with db.engine.begin() as conn:
        migration.op = Operations(MigrationContext.configure(conn))
        migration.downgrade()
        migration.upgrade()


def test_awareness_is_directional_and_discovery_does_not_rewrite_hearing(db, config, runtime, fake):
    from chatbot.observations import notices, perceived_cast

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)

        def change(observer, target, noticed):
            roster = client.get(f"/api/role/stories/{story}/roster").json()
            revision = next(p["revision"] for p in roster if p["id"] == observer)
            result = client.patch(
                f"/api/role/actors/{observer}/awareness/{target}",
                json={"noticed": noticed, "expected_revision": revision},
            )
            assert result.status_code == 200, result.text

        change(actors[0], player, False)
        change(actors[1], player, False)
        roster = client.get(f"/api/role/stories/{story}/roster?actor_id={actors[0]}").json()
        assert player not in {p["id"] for p in roster}
        with db.session() as s:
            assert notices(s.get(Conversation, player), s.get(Conversation, actors[0]))
            assert player not in {p["id"] for p in perceived_cast(s, s.get(Conversation, actors[0]))}
        # The hidden player overhears a public statement without the speaker detecting them.
        heard = client.post(
            f"/api/role/stories/{story}/speech-events",
            json={
                "request_id": "eavesdrop",
                "actor_id": actors[0],
                "text": "门后藏着钥匙。",
                "observations": [{"actor_id": player, "text": "门后", "speaker_known": False}],
            },
        )
        assert heard.status_code == 201, heard.text
        eid = heard.json()["id"]
        change(actors[0], player, True)
        with db.session() as s:
            assert notices(s.get(Conversation, actors[0]), s.get(Conversation, player))
            assert not notices(s.get(Conversation, actors[1]), s.get(Conversation, player))
            assert observed_record(s, player, eid).text == "门后"
            assert observed_record(s, player, eid).actor_id is None
        # Recognized public speech reveals the player to the second NPC, without filling old gaps.
        runtime.submit(player, config.workspace_owner, "我在这里。", "reveal")
        with db.session() as s:
            assert notices(s.get(Conversation, actors[1]), s.get(Conversation, player))
            assert observed_record(s, player, eid).text == "门后"


async def test_idle_actor_prompts_do_not_name_hidden_listener(db, config, runtime, fake):
    from chatbot.agents import Agents
    from chatbot.storage import Task, now

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
    with db.session.begin() as s:
        for aid in actors:
            actor = s.get(Conversation, aid)
            actor.current_state = {**actor.current_state, "unnoticed_ids": [player]}
            s.add(RoleMemory(story_id=story, actor_id=aid, kind="goal", content="提议先休息。"))
        c = s.get(Conversation, player)
        c.current_state = {**c.current_state, "presence": {"until": now().timestamp() + 300, "visible": True}}
        queued = runtime.enqueue(
            s,
            "respond",
            config.workspace_owner,
            {"text": "空闲推进", "autonomous": True, "audience": [player, *actors]},
            session_id=player,
            epoch=c.epoch,
        )
        task_id = queued.id
    task = runtime.claim("idle-perception")
    assert task.id == task_id
    await Agents(db, config, runtime, fake).respond(task)
    for role, messages, _ in fake.calls:
        if role in {"role_intent", "dialogue", "actor_review"}:
            assert player not in json.dumps(messages, ensure_ascii=False)
            assert "unnoticed_ids" not in json.dumps(messages, ensure_ascii=False)
    assert any(role == "role_intent" for role, _, _ in fake.calls)
    with db.session() as s:
        assert s.get(Task, task_id)


async def test_unseen_object_action_commits_without_player_caption_or_final_text(db, config, runtime, fake):
    from chatbot.agents import Agents
    from chatbot.scene_state import objects
    from chatbot.storage import Message

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        item = client.post(
            f"/api/role/stories/{story}/objects", json={"request_id": "hidden-action", "name": "铜盒"}
        ).json()
    with db.session.begin() as s:
        p = s.get(Conversation, player)
        p.current_state = {**p.current_state, "unnoticed_ids": actors}
    fake.replies["role_intent"] = {
        "act": "object_action",
        "purpose": "拿起铜盒",
        "source_ids": [],
        "addressee_id": None,
        "wait_for_player": True,
        "retrieval": "context",
        "deception": False,
        "object_action": {"verb": "take", "object_id": item["id"], "expected_version": 1},
    }
    runtime.submit(player, config.workspace_owner, "有人在吗？", "unseen-action")
    task = runtime.claim("unseen-test")
    result = await Agents(db, config, runtime, fake).respond(task)
    assert result["answer"] == "" and result["segments"] == [] and result["actor_ids"] == []
    runtime.finish(task.id, task.lease_version, result)
    with db.session() as s:
        assert objects(s, story)[0]["holder_id"] in actors
        assert objects(s, story, player)[0]["holder_id"] is None
        assert not s.scalar(select(Message).where(Message.task_id == task.id, Message.role == "assistant"))
