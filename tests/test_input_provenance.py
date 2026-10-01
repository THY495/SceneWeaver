import json
from types import SimpleNamespace

from chatbot.evidence import model_evidence
from chatbot.npc.audit import retrieval_message, review_message
from chatbot.observability.timing import TimingTrace, record_request
from chatbot.providers import estimate_tokens
from chatbot.role_policy import review_grounding
from chatbot.storage import now


def test_review_and_retrieval_provenance_match_inputs_without_changing_them(tmp_path):
    documents = [
        {
            "id": "doc-1",
            "text": "secret-document",
            "document": "private-title",
            "section": "s",
            "excerpt_truncated": True,
        }
    ]
    info = {
        "profile": {"core": "secret-profile"},
        "observations": [],
        "scene_facts": {"fact": {"event_id": "event-1", "value": "secret-fact"}},
        "active_goals": {"goal-1": "secret-goal"},
    }
    speech = {"claims": [{"source_id": "S1"}]}
    sources = {"S1": "secret-document"}
    expected = review_grounding(info, model_evidence(documents), speech, sources)
    task = SimpleNamespace(id="audit", attempt=1, lease_version=1, created_at=now(), updated_at=now())
    trace = TimingTrace(task)
    with trace.activate():
        review = review_message(
            info, model_evidence(documents), speech, sources, actor_id="actor-1", documents=documents
        )
        assert json.loads(review["content"]) == expected
        retrieval = {"role": "user", "content": "secret-retrieved-material"}
        assert retrieval_message(retrieval, [*documents, {"id": "doc-2"}], documents) is retrieval
        record_request(
            {"messages": [retrieval, review], "max_tokens": 10, "stream": False},
            role="actor_review",
            usage_id="mock",
            estimate=estimate_tokens,
        )
    components = trace.requests[0]["components"]
    assert components[0]["source_groups"]["capacity_omitted"]["ids"] == ["doc-2"]
    assert components[0]["source_groups"]["excerpt_truncated"]["ids"] == ["doc-1"]
    assert components[1]["source_groups"]["confirmed_events"]["ids"] == ["event-1"]
    assert components[1]["source_groups"]["subjective_goals"]["ids"] == ["goal-1"]
    assert components[1]["source_groups"]["references"]["ids"] == ["S1"]
    assert "secret" not in json.dumps(trace.requests)
    (tmp_path / "source-ui-fixture.json").write_text(json.dumps(trace.requests), encoding="utf-8")
