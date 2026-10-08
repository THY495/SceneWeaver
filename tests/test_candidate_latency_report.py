from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def reporter():
    path = Path(__file__).resolve().parents[1] / "scripts" / "analyze_candidate_latency.py"
    spec = spec_from_file_location("analyze_candidate_latency", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def span(name, start, duration, *, attributes=None, marks=None):
    return {
        "name": name,
        "start_ms": start,
        "duration_ms": duration,
        "attributes": attributes or {},
        "marks": marks or {},
    }


def test_candidate_chain_uses_wall_clock_intervals_and_handles_no_tts():
    spans = [
        span("npc.perform", 0, 150, attributes={"actor_id": "a", "step": 0}),
        span("npc.claim_validation", 10, 1, attributes={"local_issue_count": 1}),
        span("npc.claim_validation", 20, 1, attributes={"local_issue_count": 0}),
        span(
            "npc.semantic_review_result",
            40,
            1,
            attributes={"review_accepted": False, "review_issue_count": 1},
        ),
        span("npc.claim_validation", 80, 1, attributes={"local_issue_count": 0}),
        span(
            "npc.semantic_review_result",
            120,
            1,
            attributes={"review_accepted": True, "review_issue_count": 0},
        ),
        span("director.plan", 151, 20),
    ]
    report = reporter().summarize(spans)
    actor = report["performances"][0]
    assert actor["local_rejections"] == 1
    assert actor["review_rejections"] == 1
    assert actor["approved"] is True
    assert actor["local_recovery_intervals_ms"] == [10]
    assert actor["recovery_intervals_ms"] == [80]
    assert actor["audio_discarded"] == 0
    assert report["totals"]["director_plans"] == 1


def test_parallel_candidates_follow_parent_ids_not_overlapping_intervals():
    first = {**span("npc.perform", 0, 100), "id": "a"}
    second = {**span("npc.perform", 1, 200), "id": "b"}
    review = {
        **span("npc.semantic_review_result", 50, 1, attributes={"review_accepted": True}),
        "parent_id": "a",
    }
    report = reporter().summarize([first, second, review])
    assert [p["semantic_reviews"] for p in report["performances"]] == [1, 0]
    assert report["candidate_attribution"]["parent_chain"] == 1
    legacy_spans = [
        {k: v for k, v in s.items() if k not in {"id", "parent_id"}} for s in [first, second, review]
    ]
    legacy = reporter().summarize(legacy_spans)
    assert [p["semantic_reviews"] for p in legacy["performances"]] == [0, 0]
    assert legacy["candidate_attribution"]["ambiguous"] == 1


def test_nested_director_retry_is_one_planning_interval():
    outer = {**span("director.plan", 0, 100), "id": "outer"}
    retry = {**span("director.plan", 50, 50), "id": "retry", "parent_id": "outer"}
    report = reporter().summarize([outer, retry])
    assert report["totals"]["director_plans"] == 1
    assert report["totals"]["director_plan_spans_including_nested_retries"] == 2


def test_provider_approval_followed_by_scheduling_rejection_is_not_model_rejection():
    report = reporter().summarize(
        [
            span("npc.perform", 0, 100),
            span(
                "npc.semantic_review_result",
                10,
                1,
                attributes={"review_accepted": False, "review_provider_accepted": True},
                marks={"semantic_rejected": 0, "director_addressee_mismatch": 0},
            ),
            span(
                "npc.semantic_review_result",
                20,
                1,
                attributes={"review_accepted": False, "review_provider_accepted": False},
            ),
            span("npc.semantic_review_result", 30, 1, attributes={"review_accepted": True}),
        ]
    )
    totals = report["totals"]
    assert totals["review_rejections"] == 2
    assert totals["provider_rejections"] == 1
    assert totals["provider_verdict_unknown"] == 1
    assert totals["scheduling_rejections"]["director_addressee_mismatch"] == 1
