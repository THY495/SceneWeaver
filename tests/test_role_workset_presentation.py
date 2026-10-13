import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from support.story import launch

import chatbot.role_memory as role_memory
from chatbot.api import create_app
from chatbot.context import ContextPressure
from chatbot.observations import append_record, record_data, visible_records
from chatbot.role_memory import RoleMemoryService, _workset_observation
from chatbot.storage import Conversation, RoleMemory, RoleSnapshot, SceneRecord, Task, uid


@pytest.mark.parametrize("repeats, fits", [(30, True), (60, False)])
def test_required_speech_can_fit_without_waiting_for_pending_reflection(
    db, config, runtime, fake, monkeypatch, repeats, fits
):
    config.product = "roleplay"
    config.context_hard_tokens = 24000
    config.context_soft_tokens = 16000
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
    keeper, listener, _ = actors
    with db.session.begin() as s:
        visitor, actor, other = (s.get(Conversation, aid) for aid in (player, keeper, listener))
        original = append_record(
            s,
            story_id=story,
            kind="speech",
            actor=visitor,
            text="银钥匙在钟室抽屉；这只是转述，尚未核实。",
            observer_ids=[keeper, player],
            dedupe="original",
        )
        s.add(
            RoleSnapshot(
                actor_id=keeper,
                covered_seq=original.seq,
                privacy_version=actor.privacy_version,
                summary={"summary": "访客转述银钥匙的位置，尚未核实。"},
                source_ids=[original.id],
            )
        )
        goal = RoleMemory(
            id=uid(),
            story_id=story,
            actor_id=keeper,
            kind="commitment",
            content="保留疑问，先核实再更新登记。",
            source_ids=[original.id],
            context_source_ids=[original.id],
        )
        s.add(goal)
        correction = append_record(
            s,
            story_id=story,
            kind="speech",
            actor=visitor,
            text="我纠正：银钥匙在档案柜；这仍是转述，不是亲眼看见。",
            observer_ids=[keeper, player],
            dedupe="correction",
        )
        speech_ids = []
        for index in range(2):
            parts = ["这条说法尚未核实，先查登记。" * repeats, "我只保留疑问，没有完成检查。" * repeats]
            body = "\n\n".join(parts)
            assert len(body) <= 2400
            payload = {
                "intent": {"act": "speak", "purpose": "保留疑问", "source_ids": [correction.id]},
                "claims": [
                    {
                        "mode": "reported",
                        "text": parts[0],
                        "quote": correction.text,
                        "source_id": correction.id,
                    }
                ],
                "spoken_parts": parts,
                "performance": [
                    {
                        "text": part,
                        "pose": "thinking",
                        "expression": "concerned",
                        "voice_direction": "谨慎，缓慢",
                    }
                    for part in parts
                ],
            }
            speech = append_record(
                s,
                story_id=story,
                kind="speech",
                actor=actor,
                text=body,
                payload=payload,
                observer_ids=[keeper, player],
                dedupe=f"speech-{index}",
            )
            speech_ids.append(speech.id)
            if index == 0:
                partial = append_record(
                    s,
                    story_id=story,
                    kind="speech",
                    actor=other,
                    text="银钥匙的事我听说了；柜子暗号是四一七。",
                    payload={"intent": {"secret": "四一七"}},
                    observer_ids=[keeper, listener],
                    perceptions={keeper: {"text": "银钥匙的事我听说了", "speaker_known": False}},
                    dedupe="partial",
                )
    task = runtime.submit(
        player,
        config.workspace_owner,
        "我关于银钥匙的前后说法是什么？请引用。",
        "current",
        channel="private",
        addressees=[keeper],
    )
    # A real queued reflection stays pending throughout both service calls.
    service = RoleMemoryService(db, config, runtime, fake)
    pending = service.schedule(keeper, config.workspace_owner, parent_id=task.id, force=True)
    assert pending
    with db.session() as s:
        before = {e.id: deepcopy(e.payload) for e in s.scalars(select(SceneRecord))}
        required = {e.id: record_data(e, viewer_id=keeper) for e in visible_records(s, keeper)}
    projection = role_memory._workset_observation
    with monkeypatch.context() as old:
        old.setattr(
            role_memory, "_workset_observation", lambda e, actor_id: record_data(e, viewer_id=actor_id)
        )
        with pytest.raises(ContextPressure):
            service.context(task, keeper, reserve_tokens=10000)
    assert role_memory._workset_observation is projection
    if not fits:
        with pytest.raises(ContextPressure):
            service.context(task, keeper, reserve_tokens=10000)
    else:
        messages, info = service.context(task, keeper, reserve_tokens=10000)
        assert info["tokens"] <= 14000 and info["snapshot_id"]
        tail = json.loads(messages[2]["content"].partition("：")[2])
        selected = {e["id"]: e for e in tail}
        must_keep = {correction.id, partial.id, task.payload["event_id"], *speech_ids}
        assert must_keep <= selected.keys()
        assert [e["seq"] for e in tail] == sorted(e["seq"] for e in tail)
        for event_id in must_keep:
            assert {k: v for k, v in selected[event_id].items() if k != "detail"} == {
                k: v for k, v in required[event_id].items() if k != "detail"
            }
        for event_id in speech_ids:
            detail = selected[event_id]["detail"]
            assert detail["claims"] == before[event_id]["claims"]
            assert detail["intent"] == before[event_id]["intent"]
            assert "performance" not in detail and "spoken_parts" not in detail
        heard = selected[partial.id]
        assert heard["actor_id"] is None and heard["speaker"] == "未辨认的声音"
        assert heard["text"] == "银钥匙的事我听说了" and not heard["perception"]["complete"]
        assert heard["detail"] == {} and "四一七" not in json.dumps(messages, ensure_ascii=False)
        assert goal.id in info["active_goals"]
        assert [e["id"] for e in info["player_statement_history"]["items"]] == [original.id, correction.id]
        # Grounding uses complete scoped records, not the prompt presentation projection.
        full = {e["id"]: e for e in info["observations"]}
        assert full[speech_ids[0]]["detail"]["spoken_parts"] == before[speech_ids[0]]["spoken_parts"]
    with db.session() as s:
        assert before == {e.id: e.payload for e in s.scalars(select(SceneRecord))}
        assert s.get(Task, pending.id).status == "queued"
    assert not fake.calls


@pytest.mark.parametrize(
    "change",
    [
        "other_actor",
        "other_kind",
        "no_top",
        "unequal",
        "invalid_type",
        "space_difference",
        "missing_segments",
        "different_segments",
    ],
)
def test_workset_projection_keeps_nonidentical_or_unreconstructable_content(monkeypatch, change):
    actor = "keeper"
    record = SimpleNamespace(kind="speech", actor_id=actor)
    value = dict(
        kind="speech",
        text="First\n\nSecond",
        appearance={"variant": "smile"},
        performance=[{"text": "First"}, {"text": "Second"}],
        detail={
            "appearance": {"variant": "smile"},
            "performance": [{"text": "First"}, {"text": "Second"}],
            "spoken_parts": ["First", "Second"],
            "intent": {"act": "speak"},
            "claims": [{"quote": "First"}],
        },
    )
    if change == "other_actor":
        record.actor_id = "other"
    elif change == "other_kind":
        record.kind = "action"
    elif change == "no_top":
        value.pop("appearance")
        value.pop("performance")
    elif change == "unequal":
        value["appearance"] = {"variant": "angry"}
        value["performance"] = [{"text": "Different"}]
    elif change == "invalid_type":
        value["appearance"] = value["detail"]["appearance"] = "invalid"
        value["performance"] = value["detail"]["performance"] = ["invalid"]
    elif change == "space_difference":
        value["text"] = "First \n\nSecond"
    elif change == "missing_segments":
        value.pop("performance")
    elif change == "different_segments":
        value["performance"] = [{"text": "First\n\nSecond"}]
    original = deepcopy(value)
    monkeypatch.setattr(role_memory, "record_data", lambda *args, **kwargs: value)
    result = _workset_observation(record, actor)
    assert value == original
    if change in {"other_actor", "other_kind", "no_top", "unequal", "invalid_type"}:
        assert result == original
    else:
        assert result["detail"]["spoken_parts"] == original["detail"]["spoken_parts"]
        assert result["text"] == original["text"]
        assert result["detail"]["claims"] == original["detail"]["claims"]
