import importlib
import json
from pathlib import Path


async def test_control_fixture_recovery_never_replays_committed_actor(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    probe = importlib.import_module("probe_actor_pipeline")
    public = json.dumps({"cast": [{"name": name, "id": aid} for name, aid in
        [("阿岚", "alan"), ("沈舟", "shen"), ("林澈", "lin")]]})
    plan = await probe.fixed_plan(None, "task", None, "beat", public_context=public,
        completed_jobs=[{"actor": "alan", "event_id": "already-committed", "objective": "已经发表"}])
    assert [job.actor for job in plan.jobs] == ["shen", "lin"]
    assert plan.jobs[0].depends_on == []
    assert plan.jobs[1].depends_on == [plan.jobs[0].id]


async def test_combined_reuse_fixture_preserves_typed_dependencies_and_ai_branches(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    probe = importlib.import_module("probe_beat_reuse")
    public = json.dumps({"cast": [{"name": name, "id": aid} for name, aid in
        [("阿岚", "alan"), ("沈舟", "shen"), ("林澈", "lin"), ("玩家", "player")]]})
    plan = await probe.opening_plan(None, "task", None, "beat", public_context=public, typed_responses=True)
    assert plan.jobs[0].grounded_response is None
    assert plan.jobs[1].grounded_response.job_id == plan.jobs[0].id
    assert plan.jobs[2].grounded_response.job_id == plan.jobs[1].id
    assert [job.depends_on for job in plan.jobs] == [[], ["ask-player"], ["shen-answer"]]
    recipe = probe.fixture(exercise_routes=True)
    edges = {(edge["source"], edge["target"]): edge for edge in recipe["narrative"]["edges"]}
    for enabled in [False, True]:
        source = "opening"
        for target, message in probe.route_steps(enabled):
            assert message and edges[(source, target)]["trigger"] == "ai"
            assert edges[(source, target)]["condition"]
            source = target
        assert next(node for node in recipe["narrative"]["nodes"] if node["id"] == source)["ending"]
    assert all(edge["trigger"] == "manual" for edge in probe.fixture()["narrative"]["edges"])


async def test_probe_uses_player_continuation_to_apply_grounded_exit(db, config, runtime, fake, monkeypatch):
    from fastapi.testclient import TestClient
    from test_auto_routing import prepare

    from chatbot.api import create_app
    from chatbot.auto_routing import assess
    from chatbot.providers import Provider

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    probe = importlib.import_module("probe_beat_reuse")
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run, task, path = prepare(client, runtime, db)
        fake.replies["scene_routing"] = {"assessments": [{"edge_id": "leave", "status": "met",
            "evidence": [{"event_id": task.payload["event_id"], "quote": "我决定去码头。"}],
            "reason": "明确自行离开"}]}
        await assess(db, fake, task)
        runtime.finish(task.id, task.lease_version, {})
        frame = client.get(path).json()
        assert frame["node_id"] == "start" and frame["auto_ready"]
        worker_provider = Provider(db, config, runtime)
        try:
            assert probe.apply_player_continuation(config, db, run["story_id"])["node_id"] == "end"
            assert not worker_provider.http.is_closed
            assert client.get(path).json()["node_id"] == "end"
        finally:
            await worker_provider.close()


async def test_preflight_comparison_binds_actual_question_without_replaying_committed_reply(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    probe = importlib.import_module("probe_player_action_preflight")
    public = json.dumps({"cast": [{"name": name, "id": aid} for name, aid in
        [("阿岚", "alan"), ("沈舟", "shen"), ("林澈", "lin"), ("旅人", "player")]]})
    plan = await probe.question_plan(None, "task", None, "beat", public_context=public,
                                     current_player_event_id="actual-question")
    assert len(plan.jobs) == 1 and plan.jobs[0].actor == "shen"
    assert len(plan.jobs[0].id) == 80 and plan.jobs[0].addressees == ["player"]
    assert plan.jobs[0].grounded_response.event_id == "actual-question"
    assert plan.jobs[0].grounded_response.job_id is None
    recovered = await probe.question_plan(None, "task", None, "beat", public_context=public,
        current_player_event_id="actual-question", completed_jobs=[{"actor": "shen", "event_id": "published"}])
    assert recovered.jobs == []
    recipe = probe.probe_fixture()
    keeper = next(p for p in recipe["people"] if p["profile"]["name"] == "沈舟")
    assert "码头值班员" in keeper["profile"]["core"]
    entry = next(n for n in recipe["narrative"]["nodes"] if n["id"] == "opening")
    assert entry["direction"]["beats"][0]["participants"] == [keeper["alias"]]
    assert entry["direction"]["beats"][0]["max_segments"] == 1
