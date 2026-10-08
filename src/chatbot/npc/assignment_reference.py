"""Fresh recovery reviews select authorized evidence; references never prove entailment."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..provenance import source_class


class ReferenceCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=1000)
    basis: str = Field(max_length=1000)
    supported: bool
    premise_kind: Literal[
        "world_fact",
        "past_action",
        "past_inaction",
        "acquisition",
        "current_uncertainty",
        "reported_statement",
        "expression",
    ]
    support_extent: Literal["full", "partial", "none", "not_needed"]
    source_ref: str | None = Field(max_length=80)


class ExpressionCheck(ReferenceCheck):
    premise_kind: Literal["expression", "current_uncertainty"]
    support_extent: Literal["not_needed"]
    source_ref: None


class GroundedCheck(ReferenceCheck):
    support_extent: Literal["full"]
    source_ref: str = Field(min_length=1, max_length=80)


class UnsupportedCheck(ReferenceCheck):
    support_extent: Literal["partial", "none"]
    supported: Literal[False]


class ReferenceReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checks: list[ExpressionCheck | GroundedCheck | UnsupportedCheck] = Field(max_length=12)
    accepted: bool
    issues: list[str] = Field(default_factory=list, max_length=6)
    reply_requirement: Literal["required", "optional", "none"] | None = None


def evidence_catalog(info):
    """Indexes cover original authorized records, including long records without truncation."""
    records = []

    def add(identifier, kind, location, body, encoding):
        for start in range(0, len(body), 750):
            end = min(start + 1500, len(body))
            records.append(
                {
                    "id": f"{identifier}:{start}",
                    "kind": kind,
                    "location": location,
                    "slice_start": start,
                    "slice_end": end,
                    "encoding": encoding,
                    "text": body[start:end],
                }
            )
            if end == len(body):
                break

    for field, kind in (
        ("profile", "profile"),
        ("scene_facts", "confirmed_event"),
        ("known_objects", "item_observation"),
        ("known_agreements", "agreement"),
    ):
        if info.get(field):
            add(
                field, kind, f"/authorized_state/{field}", json.dumps(info[field], ensure_ascii=False), "json"
            )
    kinds = {
        "utterance": "utterance",
        "confirmed_event": "confirmed_event",
        "object_observation": "item_observation",
        "item_action_result": "item_observation",
        "relationship_event": "agreement",
    }
    for index, event in enumerate(info.get("observations", [])):
        kind = kinds.get(source_class(event))
        if kind and event.get("text"):
            add(
                f"observed-{index}",
                kind,
                f"/authorized_state/observations/{index}/text",
                event["text"],
                "text",
            )
    return records


def reference_messages(messages, records, failure_codes):
    """Keep the original task/state; do not anchor the next verdict to a bad prior review."""
    system = (
        messages[0]["content"]
        .replace(
            "每条必填premise_kind、support_extent、source_kind、source_quote。",
            "每条必填premise_kind、support_extent、source_ref。",
        )
        .replace(
            "source_quote摘authorized_state对应资料的连续原文。",
            "source_ref选择evidence_catalog中实际支持该项的id，程序由该记录导出来源类别和连续原文。",
        )
        .replace("not_needed、source_kind=expression、source_quote=null。", "not_needed、source_ref=null。")
    )
    system += (
        "上一份审查被本地证据字段校验拒绝；重新审核同一任务和同一授权资料，不修改任务或增加知识。"
        "evidence_catalog仅是原授权状态的索引，不是新增事实。location指资料位置；"
        "encoding=json时切片指该位置资料的JSON序列化，text时指原文；slice_start/end限定连续片段。"
        "记录存在不能证明任务断言；只有该片段完整支持该项含义才填full。"
        "无需事实依据的表达或当前未知填not_needed、source_ref=null；无据或只部分支持须supported=false。"
        "旧审查不提供给你；可重新判定并拒绝真实无据或接收者冲突的任务。"
    )
    data = json.loads(messages[1]["content"])
    data["evidence_catalog"] = [
        {key: value for key, value in record.items() if key != "text"} for record in records
    ]
    data["runtime_evidence_errors"] = sorted(set(failure_codes))
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(data, ensure_ascii=False, sort_keys=True)},
    ]


def normalize_reference(value, records):
    """Derive only citation fields; preserve supported/accepted, then require normal guards."""
    review = ReferenceReview.model_validate(value)
    lookup = {record["id"]: record for record in records}
    checks = []
    for check in review.checks:
        record = lookup.get(check.source_ref)
        kind = (
            record["kind"]
            if record
            else (
                "expression" if check.source_ref is None and check.support_extent == "not_needed" else "none"
            )
        )
        checks.append(
            {
                "text": check.text,
                "basis": check.basis,
                "supported": check.supported,
                "premise_kind": check.premise_kind,
                "support_extent": check.support_extent,
                "source_kind": kind,
                "source_quote": record["text"] if record else None,
            }
        )
    return {
        "checks": checks,
        "accepted": review.accepted,
        "issues": review.issues,
        "reply_requirement": review.reply_requirement,
    }
