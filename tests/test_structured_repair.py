"""Format repair must preserve the review decision before any result can be used."""

import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from chatbot.observability.timing import TimingTrace, annotate, measure
from chatbot.providers import (
    Provider,
    ProviderError,
    StructuredOutputError,
    review_output_diagnostics,
    review_repair_errors_supported,
    review_repair_preserves,
    review_repairable,
)
from chatbot.schemas import ActorReview
from chatbot.storage import Operation, Usage, now


def review():
    return {
        "accepted": False,
        "checks": [
            {
                "text": "我昨天见过他",
                "basis": "没有已确认的昨日相见记录",
                "supported": False,
                "reason_code": "unrecognized_code",
            }
        ],
        "issues": ["过去经历无依据"],
        "delivery": {"addressee_id": "player", "expects_reply": False, "response_kind": "answer"},
    }


def test_review_repair_allows_only_non_decision_format_change():
    original = review_repairable(json.dumps(review(), ensure_ascii=False))
    repaired = review()
    repaired["checks"][0]["reason_code"] = "other"
    repaired["checks"][0]["basis"] = "无昨日相见记录"
    repaired["delivery"]["remaining_request"] = None
    assert review_repair_preserves(original, repaired)


def test_review_repair_rejects_any_changed_verdict_or_missing_judgement():
    original = review_repairable(json.dumps(review(), ensure_ascii=False))
    changed = review()
    changed["accepted"] = True
    assert not review_repair_preserves(original, changed)
    changed = review()
    changed["checks"][0]["supported"] = True
    assert not review_repair_preserves(original, changed)
    changed = review()
    changed["issues"] = []
    assert not review_repair_preserves(original, changed)
    assert review_repairable('{"accepted":true,"delivery":{}}') is None
    assert review_repairable('{"accepted":') is None


def test_only_non_decision_validation_errors_are_repairable():
    assert review_repair_errors_supported([
        {"path": "checks.0.reason_code", "type": "literal_error"},
        {"path": "checks.1.basis", "type": "string_too_long"},
    ])
    assert not review_repair_errors_supported([
        {"path": "delivery.response_kind", "type": "literal_error"}
    ])
    assert not review_repair_errors_supported([
        {"path": "checks.0.supported", "type": "missing"}
    ])


@pytest.mark.asyncio
async def test_reviewer_repairs_own_result_without_resending_evidence(monkeypatch):
    provider = Provider.__new__(Provider)
    provider.runtime = SimpleNamespace(check=lambda _: SimpleNamespace(lease_version=3))
    requests = []
    committed = []
    original = review()
    repaired = review()
    repaired["checks"][0]["reason_code"] = "other"

    async def call(_, role, messages, **kwargs):
        requests.append((role, messages, kwargs))
        if len(requests) == 1:
            raise StructuredOutputError(json.dumps(original, ensure_ascii=False), [
                {"path": "checks.0.reason_code", "type": "literal_error"}
            ])
        return repaired

    monkeypatch.setattr(provider, "_chat_once", call)
    monkeypatch.setattr(provider, "_save_operation", lambda *args: committed.append(args))
    result = await provider.chat(
        "task", "actor_review", [{"role": "user", "content": "full-evidence-marker"}],
        operation="review:1",
    )
    assert result == repaired
    assert len(requests) == 2
    assert "full-evidence-marker" not in json.dumps(requests[1][1])
    assert requests[1][2]["operation"] is None
    assert committed == [("task", 3, "review:1", repaired)]


@pytest.mark.asyncio
async def test_reviewer_rejects_repair_that_changes_verdict(monkeypatch):
    provider = Provider.__new__(Provider)
    provider.runtime = SimpleNamespace(check=lambda _: SimpleNamespace(lease_version=3))
    original = review()
    altered = review()
    altered["accepted"] = True
    altered["checks"][0]["reason_code"] = "other"
    committed = []
    calls = 0

    async def call(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise StructuredOutputError(json.dumps(original, ensure_ascii=False), [
                {"path": "checks.0.reason_code", "type": "literal_error"}
            ])
        return altered

    monkeypatch.setattr(provider, "_chat_once", call)
    monkeypatch.setattr(provider, "_save_operation", lambda *args: committed.append(args))
    with pytest.raises(ProviderError, match="改变了原审查结论"):
        await provider.chat("task", "actor_review", [], operation="review:1")
    assert calls == 2
    assert committed == []


@pytest.mark.asyncio
async def test_unreadable_review_rechecks_same_evidence_once_without_actor_regeneration(monkeypatch):
    provider = Provider.__new__(Provider)
    provider.runtime = SimpleNamespace(check=lambda _: SimpleNamespace(lease_version=3))
    committed = []
    calls = []
    clean = {
        **review(),
        "checks": [],
        "issues": [],
        "accepted": True,
    }

    async def call(_task, role, messages, **kwargs):
        calls.append((role, messages, kwargs))
        if len(calls) == 1:
            raise StructuredOutputError('{"accepted":', [{"path": "", "type": "json_invalid"}])
        return clean

    monkeypatch.setattr(provider, "_chat_once", call)
    monkeypatch.setattr(provider, "_save_operation", lambda *args: committed.append(args))
    result = await provider.chat(
        "task", "actor_review", [{"role": "user", "content": "same-candidate-and-evidence"}],
        operation="review:1",
    )
    assert result == clean
    assert len(calls) == 2
    assert calls[0][1] == calls[1][1]
    assert calls[0][2]["operation"] == calls[1][2]["operation"] == "review:1"
    assert committed == []


@pytest.mark.asyncio
async def test_twice_unreadable_review_fails_closed(monkeypatch):
    provider = Provider.__new__(Provider)
    provider.runtime = SimpleNamespace(check=lambda _: SimpleNamespace(lease_version=3))
    calls = 0

    async def call(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise StructuredOutputError('{"accepted":', [{"path": "", "type": "json_invalid"}])

    monkeypatch.setattr(provider, "_chat_once", call)
    with pytest.raises(StructuredOutputError):
        await provider.chat("task", "actor_review", [], operation="review:1")
    assert calls == 2


@pytest.mark.parametrize("answer,shape,accepted", [
    ('{"accepted":', "invalid_json", "unavailable"),
    ('[]', "array", "unavailable"),
    ('"private reply"', "string", "unavailable"),
    ('null', "null", "unavailable"),
    ('true', "boolean", "unavailable"),
    ('3', "number", "unavailable"),
    ('{}', "object", "missing"),
    ('{"accepted":null}', "object", "null"),
    ('{"accepted":"private verdict"}', "object", "string"),
    ('{"accepted":false}', "object", "boolean"),
])
def test_invalid_review_shape_is_content_free_and_never_supplies_verdict(answer, shape, accepted):
    result = review_output_diagnostics(answer)
    assert result["review_output_json_shape"] == shape
    assert result["review_output_accepted_kind"] == accepted
    assert "private" not in json.dumps(result)
    assert "accepted" not in result
    assert review_repairable(answer) is None


def test_wrapped_review_is_diagnostic_only_and_arbitrary_keys_are_not_recorded():
    original = review()
    wrapper = json.dumps({"private-key-marker": original})
    metrics = review_output_diagnostics(wrapper)
    assert metrics == {
        "review_output_json_shape": "object", "review_output_accepted_kind": "missing",
        "review_output_checks_kind": "missing", "review_output_issues_kind": "missing",
        "review_output_delivery_kind": "missing", "review_output_single_wrapped_review": True,
    }
    assert review_repairable(wrapper) is None
    sample = SimpleNamespace(id="shape", attempt=1, lease_version=1, created_at=now(), updated_at=now())
    trace = TimingTrace(sample)
    with trace.activate(), measure("model.http"):
        annotate(**metrics, raw_review=wrapper, arbitrary_key="private-key-marker")
    assert trace.spans[0]["attributes"] == metrics
    assert "private" not in json.dumps(trace.spans)
    assert not review_output_diagnostics(json.dumps({"a": original, "b": original}))[
        "review_output_single_wrapped_review"]


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [False, True])
async def test_invalid_review_diagnostics_preserve_real_provider_retry_usage_and_cache(
    db, config, runtime, recover,
):
    conversation = runtime.create_session()
    runtime.submit(conversation.id, "local", "你好", "shape-check")
    task = runtime.claim("shape-worker")
    trace = TimingTrace(task)
    calls = []
    clean = {**review(), "accepted": False, "checks": [], "issues": ["待确认"]}
    failed = {"private-key-marker": {**clean, "issues": ["private-text-marker"]}}

    def respond(request):
        calls.append(json.loads(request.content))
        value = clean if recover and len(calls) == 2 else failed
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        })

    provider = Provider(db, config, runtime)
    await provider.http.aclose()
    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        with trace.activate():
            if recover:
                actual = await provider.chat(task.id, "actor_review", [], schema=ActorReview,
                                             operation="shape-review")
                assert actual["accepted"] is False  # A successful retry does not imply approval.
            else:
                with pytest.raises(StructuredOutputError):
                    await provider.chat(task.id, "actor_review", [], schema=ActorReview,
                                        operation="shape-review")
        assert len(calls) == 2 and calls[0] == calls[1]
        attempts = [s for s in trace.spans if s["name"] == "model.attempt"]
        assert len(attempts) == 2
        assert attempts[0]["attributes"]["review_output_single_wrapped_review"] is True
        assert attempts[0]["attributes"]["review_output_accepted_kind"] == "missing"
        assert ("review_output_json_shape" in attempts[1]["attributes"]) is not recover
        trace.save(db)
        with db.session() as store:
            usage = list(store.scalars(select(Usage).where(Usage.task_id == task.id)))
            assert len(usage) == 2 and all(u.reserved == 0 for u in usage)
            assert sum(u.status == "failed" for u in usage) == (1 if recover else 2)
            assert sum(u.status == "completed" for u in usage) == int(recover)
            assert all(u.cost == 0 for u in usage)
            assert {a["attributes"]["usage_id"] for a in attempts} == {u.id for u in usage}
            operations = list(store.scalars(select(Operation).where(Operation.task_id == task.id)))
            caches = [o for o in operations if o.id == f"{task.id}:shape-review"]
            assert len(caches) == int(recover)
            stored_trace = [o.payload for o in operations if "timing_v1" in o.payload]
            assert len(stored_trace) == 1
            serialized = json.dumps(stored_trace)
            assert "private-key-marker" not in serialized and "private-text-marker" not in serialized
    finally:
        await provider.close()
