"""Offline counterexample audit of tempting review-bypass heuristics.

Diagnostic only: does not run a model, read credentials or alter publication.
Expected labels are historical annotations, not model-review verdicts or truth.
"""

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUT_KEYS = (
    "info",
    "speech",
    "sources",
    "intent",
    "cast",
    "actor_id",
    "player_request",
    "director_assignment",
)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def proposals(inputs):
    """Deliberately untrusted baselines; labels cannot affect these decisions."""
    speech, intent = inputs["speech"], inputs["intent"]
    empty = not speech["claims"]
    context = empty and intent.get("act") == "speak" and intent.get("retrieval") in (None, "context")
    stance = context and bool(
        re.match(r"^我(?:觉得|认为|想|建议|个人建议|不知道|不清楚|不想|不能)", speech["text"].strip())
    )
    return {"empty_claims": empty, "context_speech_empty_claims": context, "stance_prefix": stance}


def load_suites(paths):
    rows, manifests = [], []
    for path in sorted(paths):
        raw = path.read_bytes()
        suite = json.loads(raw)
        if suite.get("target", "speech") != "speech":
            continue
        defaults = suite.get("defaults", {})
        accepted = 0
        for item in suite.get("cases", []):
            case = {**defaults, **item}
            if not isinstance(case.get("expected_accept"), bool):
                raise ValueError(f"Missing independent label: {path.name}/{case.get('case_id')}")
            inputs = {key: case[key] for key in INPUT_KEYS if key in case}
            if (
                not isinstance(inputs.get("speech", {}).get("text"), str)
                or not isinstance(inputs.get("speech", {}).get("claims"), list)
                or not isinstance(inputs.get("intent"), dict)
            ):
                raise ValueError(f"Incomplete candidate: {path.name}/{case.get('case_id')}")
            rows.append(
                {
                    "suite": path.name,
                    "case_id": case["case_id"],
                    "input_hash": digest(inputs),
                    "expected_accept": case["expected_accept"],
                    "expected_reason_codes": case.get("expected_reason_codes", []),
                    "proposed_low_risk": proposals(inputs),
                }
            )
            accepted += 1
        manifests.append(
            {
                "file": path.name,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "cases": accepted,
                "origin": suite.get("origin", "existing-synthetic-calibration"),
            }
        )
    return rows, manifests


def summarize(rows, manifests):
    groups = defaultdict(list)
    for row in rows:
        groups[row["input_hash"]].append(row)
    conflicts = [
        {
            "input_hash": key,
            "cases": [
                {"suite": r["suite"], "case_id": r["case_id"], "expected_accept": r["expected_accept"]}
                for r in values
            ],
        }
        for key, values in sorted(groups.items())
        if len({row["expected_accept"] for row in values}) > 1
    ]
    conflicted = {row["input_hash"] for row in conflicts}
    eligible = [row for row in rows if row["input_hash"] not in conflicted]
    distinct = [values[0] for key, values in groups.items() if key not in conflicted]

    def count(values, key):
        proposed = [row for row in values if row["proposed_low_risk"][key]]
        misrouted = [row for row in proposed if not row["expected_accept"]]
        return {
            "cases": len(values),
            "proposed_low_risk": len(proposed),
            "expected_accept_in_low_risk": sum(row["expected_accept"] for row in proposed),
            "expected_reject_in_low_risk": len(misrouted),
            "counterexamples": [
                {
                    "suite": row["suite"],
                    "case_id": row["case_id"],
                    "expected_reason_codes": row["expected_reason_codes"],
                }
                for row in misrouted
            ],
        }

    keys = ("empty_claims", "context_speech_empty_claims", "stance_prefix")
    return {
        "scope": "Offline synthetic speech suites including authored shadow counterexamples; no model/DB/credentials; publication unchanged",
        "limitations": [
            "expected_accept annotations can be wrong; original reviewer verdicts are not used as truth",
            "all versions retained and same-input conflicting labels excluded from pooled counts",
            "cases were built around failures, not a representative traffic sample or hallucination rate",
            "a low-risk suggestion is not a proof; no audit is skipped and no latency savings are measured",
            "only labels and reason codes are exported, never full candidate/workset content",
        ],
        "manifests": manifests,
        "case_rows": len(rows),
        "distinct_inputs": len(groups),
        "conflicting_labels": conflicts,
        "hypotheses": {
            key: {
                "all_nonconflicting_cases": count(eligible, key),
                "distinct_nonconflicting_inputs": count(distinct, key),
            }
            for key in keys
        },
        "by_suite": {
            suite: {key: count([row for row in eligible if row["suite"] == suite], key) for key in keys}
            for suite in sorted(Counter(r["suite"] for r in rows))
        },
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    path = args.output.resolve()
    if not path.is_relative_to(ROOT / ".runtime") or path.exists():
        parser.error("Choose a fresh .runtime output file")
    source = ROOT / "evals/cases"
    paths = [*source.glob("reviewer-*.json"), source / "ferry-review-boundaries-v1.json"]
    rows, manifests = load_suites(paths)
    result = summarize(rows, manifests)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "case_rows": result["case_rows"],
                "distinct_inputs": result["distinct_inputs"],
                "conflicts": len(result["conflicting_labels"]),
                "hypotheses": {
                    key: {
                        k: v
                        for k, v in value["distinct_nonconflicting_inputs"].items()
                        if k != "counterexamples"
                    }
                    for key, value in result["hypotheses"].items()
                },
            }
        )
    )


if __name__ == "__main__":
    main()
