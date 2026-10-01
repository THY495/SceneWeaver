"""Format repair must preserve the review decision before any result can be used."""

import json
from types import SimpleNamespace

import pytest

from chatbot.providers import (
    Provider,
    ProviderError,
    StructuredOutputError,
    review_repair_errors_supported,
    review_repair_preserves,
    review_repairable,
)


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
