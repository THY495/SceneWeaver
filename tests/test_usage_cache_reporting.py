"""Missing cache evidence must not become a measured zero, even after export."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from chatbot.api import create_app
from chatbot.observability.usage import cache_reporting, cache_tokens_reported
from chatbot.providers import Provider, responses_review_usage
from chatbot.storage import Operation, Usage, uid


@pytest.mark.parametrize(
    ("usage", "reported"),
    [
        (None, False),
        ({}, False),
        ({"prompt_cache_hit_tokens": 0}, True),
        ({"prompt_cache_hit_tokens": 20}, True),
        ({"prompt_tokens_details": {"cached_tokens": 0}}, True),
        ({"prompt_tokens_details": {"cached_tokens": 20}}, True),
        ({"prompt_tokens_details": None}, False),
        ({"prompt_cache_hit_tokens": None}, False),
        ({"prompt_cache_hit_tokens": False}, False),
        ({"prompt_cache_hit_tokens": -1}, False),
        ({"prompt_cache_hit_tokens": "20"}, False),
    ],
)
def test_cache_field_presence_and_validity(usage, reported):
    assert cache_tokens_reported(usage) is reported


@pytest.mark.parametrize("details", [None, {}, {"cached_tokens": 0}, {"cached_tokens": 20}])
def test_responses_normalization_preserves_cache_absence(details):
    result = responses_review_usage(
        {"input_tokens": 100, "output_tokens": 10, "input_tokens_details": details}
    )
    assert result["prompt_tokens"] == 100 and result["completion_tokens"] == 10
    expected = isinstance(details, dict) and "cached_tokens" in details
    assert cache_tokens_reported(result) is expected
    assert ("prompt_cache_hit_tokens" in result) is expected


@pytest.mark.parametrize(
    ("usage", "reported", "tokens"),
    [
        ({"prompt_tokens": 100, "completion_tokens": 10}, False, 0),
        ({"prompt_tokens": 100, "completion_tokens": 10, "prompt_cache_hit_tokens": 0}, True, 0),
        ({"prompt_tokens": 100, "completion_tokens": 10, "prompt_cache_hit_tokens": 20}, True, 20),
        (
            {"prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 20}},
            True,
            20,
        ),
        (None, False, 0),
    ],
)
async def test_settlement_provenance_does_not_change_pricing_or_retain_body(
    db, config, runtime, usage, reported, tokens
):
    session = runtime.create_session()
    runtime.submit(session.id, "local", "测试", "cache")
    task = runtime.claim("cache-worker")
    provider = Provider(db, config, runtime)
    try:
        usage_id = provider.reserve(task.id, "dialogue", "test-model", "test", 50, price=(2, 1, 3))
        if usage is not None:
            usage = {**usage, "cost": 0.01, "private_upstream_body": "MUST_NOT_STORE"}
        provider.settle(usage_id, usage, 0.1, "completed")
        with db.session() as s:
            row = s.get(Usage, usage_id)
            operation = s.get(Operation, f"usage-cache:{usage_id}")
            price = s.get(Operation, f"usage:{usage_id}")
            assert operation.payload == {"cache_tokens_reported": reported}
            assert price.payload == {"price": [2, 1, 3]}
            assert row.cache_tokens == tokens and row.reserved == 0
            assert cache_reporting(s, [row]) == {usage_id: reported}
            assert row.cost == (0.01 if usage is not None else (34 * 2 + 50 * 3) / 1e6)
        # The provenance row is updated, rather than duplicated, if settlement repeats.
        provider.settle(usage_id, usage, 0.1, "completed")
        with db.session() as s:
            assert (
                len(list(s.scalars(select(Operation).where(Operation.id == f"usage-cache:{usage_id}")))) == 1
            )
    finally:
        await provider.close()


def test_creator_export_scopes_cache_evidence_and_preserves_legacy_unknown(db, config, fake, runtime):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run = client.post(
            "/api/role/experiments/start", json={"request_id": "cache", "template_id": "partial-hearing"}
        ).json()
        with db.session.begin() as s:
            task = runtime.enqueue(s, "npc_step", config.workspace_owner, {"story_id": run["story_id"]})
            other = runtime.enqueue(s, "npc_step", "other-owner", {})
            cases = [None, False, True, "wrong-task", "not-a-bool"]
            for evidence in cases:
                row = Usage(
                    id=uid(),
                    owner_id=config.workspace_owner,
                    task_id=task.id,
                    role="dialogue",
                    model="test",
                    cache_tokens=0,
                )
                s.add(row)
                if evidence is not None:
                    s.add(
                        Operation(
                            id=f"usage-cache:{row.id}",
                            task_id=other.id if evidence == "wrong-task" else task.id,
                            payload={
                                "cache_tokens_reported": True if evidence == "wrong-task" else evidence,
                                "private": "MUST_NOT_EXPORT",
                            },
                        )
                    )
            s.add(Usage(owner_id="other-owner", task_id=other.id, role="dialogue", model="OTHER_OWNER"))
        response = client.get(f"/api/role/experiments/runs/{run['story_id']}/record")
        assert response.status_code == 200
        record = response.json()
        reports = [row["cache_tokens_reported"] for row in record["calls"]]
        assert len(reports) == 5 and reports.count(None) == 3
        assert sum(value is False for value in reports) == 1
        assert sum(value is True for value in reports) == 1
        assert "MUST_NOT_EXPORT" not in str(record) and "OTHER_OWNER" not in str(record)
        assert all(row["cache_tokens"] == 0 for row in record["calls"])
