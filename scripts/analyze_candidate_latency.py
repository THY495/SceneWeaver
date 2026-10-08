"""Summarize candidate repair chains in an exported task timing JSON.

The intervals are observed wall-clock exposure, not counterfactual time saved by
removing a review. Private TTS may overlap a review, so provider durations are
never added to these intervals.
"""

import argparse
import json
from pathlib import Path

SCHEDULING_MARKS = ("reply_capacity_exhausted", "director_addressee_mismatch", "duplicate_npc_request")


def end(span):
    return span["start_ms"] + span["duration_ms"]


def inside(child, parent):
    return parent["start_ms"] <= child["start_ms"] and end(child) <= end(parent) + 0.1


def summarize(spans):
    by_id = {s["id"]: s for s in spans if s.get("id")}
    performers = [s for s in spans if s["name"] == "npc.perform"]
    attribution = {"parent_chain": 0, "unique_interval": 0, "ambiguous": 0, "unmatched": 0}

    def ancestors(child):
        seen = set()
        parent_id = child.get("parent_id")
        while parent_id in by_id and parent_id not in seen:
            seen.add(parent_id)
            parent = by_id[parent_id]
            yield parent
            parent_id = parent.get("parent_id")

    def owner(child):
        parent = next((s for s in ancestors(child) if s["name"] == "npc.perform"), None)
        if parent is not None:
            attribution["parent_chain"] += 1
            return parent
        matches = [s for s in performers if inside(child, s)]
        if len(matches) == 1:
            attribution["unique_interval"] += 1
            return matches[0]
        attribution["ambiguous" if matches else "unmatched"] += 1
        return None

    owned = {id(s): [] for s in performers}
    for child in spans:
        if child["name"] in {"npc.claim_validation", "npc.review_audio", "npc.semantic_review_result"}:
            parent = owner(child)
            if parent is not None:
                owned[id(parent)].append(child)
    performances = []
    for index, performer in enumerate(performers):
        children = owned[id(performer)]
        local = sorted(
            (s for s in children if s["name"] == "npc.claim_validation"),
            key=lambda s: s["start_ms"],
        )
        review = sorted(
            (s for s in children if s["name"] == "npc.review_audio"),
            key=lambda s: s["start_ms"],
        )
        semantic = sorted(
            (s for s in children if s["name"] == "npc.semantic_review_result"),
            key=lambda s: s["start_ms"],
        )
        rejected_reviews = [
            s
            for s in semantic
            if s.get("marks", {}).get("semantic_rejected") is not None
            or s.get("attributes", {}).get("review_accepted") is False
            or s.get("attributes", {}).get("review_issue_count", 0) > 0
            or s.get("attributes", {}).get("review_unsupported_count", 0) > 0
        ]
        rejected_locals = [s for s in local if s.get("attributes", {}).get("local_issue_count", 0) > 0]
        approved = next((s for s in semantic if s not in rejected_reviews), None)
        # A rejection-to-next-result span measures how long the already-observed
        # revision chain occupied the caller. It includes the next review and can
        # include overlapping audio. It is not a predicted savings amount.
        recovery_intervals = []
        for rejected in rejected_reviews:
            later = next((s for s in semantic if s["start_ms"] > rejected["start_ms"]), None)
            if later:
                recovery_intervals.append(round(end(later) - end(rejected), 1))
        local_recovery_intervals = []
        for rejected in rejected_locals:
            later = next((s for s in local if s["start_ms"] > rejected["start_ms"]), None)
            if later:
                local_recovery_intervals.append(round(end(later) - end(rejected), 1))
        performances.append(
            {
                "index": index,
                "actor_id": performer.get("attributes", {}).get("actor_id"),
                "step": performer.get("attributes", {}).get("step"),
                "start_ms": round(performer["start_ms"], 1),
                "duration_ms": round(performer["duration_ms"], 1),
                "local_candidates": len(local),
                "local_rejections": len(rejected_locals),
                "semantic_reviews": len(semantic),
                "review_rejections": len(rejected_reviews),
                "provider_rejections": sum(
                    s.get("attributes", {}).get("review_provider_accepted") is False for s in semantic
                ),
                "provider_verdict_unknown": sum(
                    "review_provider_accepted" not in s.get("attributes", {}) for s in semantic
                ),
                "scheduling_rejections": {
                    code: sum(code in s.get("marks", {}) for s in semantic) for code in SCHEDULING_MARKS
                },
                "approved": approved is not None,
                "recovery_intervals_ms": recovery_intervals,
                "local_recovery_intervals_ms": local_recovery_intervals,
                "audio_discarded": sum(
                    s.get("attributes", {}).get("audio_status") == "discarded" for s in review
                ),
            }
        )
    director_spans = [s for s in spans if s["name"] == "director.plan"]
    director_plans = sorted(
        (
            s
            for s in director_spans
            if not any(parent["name"] == "director.plan" for parent in ancestors(s))
            and not any(
                other is not s and inside(s, other) and other["duration_ms"] > s["duration_ms"]
                for other in director_spans
            )
        ),
        key=lambda s: s["start_ms"],
    )
    return {
        "performances": performances,
        "director_plans": [
            {"start_ms": round(s["start_ms"], 1), "duration_ms": round(s["duration_ms"], 1)}
            for s in director_plans
        ],
        "totals": {
            "performances": len(performances),
            "local_rejections": sum(p["local_rejections"] for p in performances),
            "review_rejections": sum(p["review_rejections"] for p in performances),
            "provider_rejections": sum(p["provider_rejections"] for p in performances),
            "provider_verdict_unknown": sum(p["provider_verdict_unknown"] for p in performances),
            "scheduling_rejections": {
                code: sum(p["scheduling_rejections"][code] for p in performances) for code in SCHEDULING_MARKS
            },
            "audio_discarded": sum(p["audio_discarded"] for p in performances),
            "director_plans": len(director_plans),
            "director_plan_spans_including_nested_retries": len(director_spans),
        },
        "candidate_attribution": attribution,
        "interpretation": "Observed intervals include subsequent generation/review and may overlap TTS; they are not additive savings estimates. Without parent IDs, overlapping ambiguous candidates are excluded rather than counted for multiple actors. Director retry spans are nested, not extra independent plans.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Exported task timing JSON with a spans array")
    parser.add_argument("--output", type=Path, help="Write JSON here; otherwise print")
    args = parser.parse_args()
    source = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(source.get("spans"), list):
        parser.error("input must contain a spans array")
    result = {
        "source": str(args.input),
        "task_duration_ms": source.get("task_duration_ms"),
        **summarize(source["spans"]),
    }
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
