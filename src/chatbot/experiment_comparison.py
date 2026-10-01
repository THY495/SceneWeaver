"""Human comparisons bind to observed runs; no inferred semantic score or model call."""

from collections import Counter
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from .experiment_archive import archive
from .experiment_initials import digest
from .storage import Conversation, ExperimentReview, Story, uid

Rating = Literal["pass", "fail", "uncertain"]


class Ratings(BaseModel):
    consistency: Rating = "uncertain"
    information_boundary: Rating = "uncertain"
    initiative: Rating = "uncertain"
    naturalness: Rating = "uncertain"


class ReviewInput(BaseModel):
    request_id: str = Field(min_length=1, max_length=80)
    left_id: str
    right_id: str
    left_signature: str
    right_signature: str
    left_ratings: Ratings
    right_ratings: Ratings
    note: str = Field(min_length=1, max_length=4000)
    left_evidence: list[str] = Field(default_factory=list, max_length=20)
    right_evidence: list[str] = Field(default_factory=list, max_length=20)


def privacy(s, sid):
    return {
        c.id: c.privacy_version for c in s.scalars(select(Conversation).where(Conversation.story_id == sid))
    }


def signature(record):
    return digest(jsonable_encoder({k: v for k, v in record.items() if k != "captured_at"}))


def summary(record):
    origin = record.get("initial")
    return {
        "story_id": record["story"]["id"],
        "story_seq": record["story"]["seq"],
        "recipe_hash": origin["receipt"]["recipe_hash"] if origin else None,
        "initial_model_settings": origin["receipt"]["model_settings"] if origin else None,
        "actual_models": sorted({c["model"] for c in record["calls"]}),
        "event_count": len(record["events"]),
        "post_initial_events": sum(e["seq"] > origin["receipt"]["initial_seq"] for e in record["events"])
        if origin
        else None,
        "task_statuses": dict(Counter(t["status"] for t in record["tasks"])),
        "decision_outcomes": dict(Counter(t["outcome"] for t in record["tasks"] if t["outcome"])),
        "call_count": len(record["calls"]),
        "cost": record["cost"],
        "active_tasks": record["active_tasks"],
    }


def comparison(s, left, right, who):
    if left == right:
        raise HTTPException(422, "请选择两段不同运行")
    a, b = archive(s, left, who), archive(s, right, who)
    sa, sb = summary(a), summary(b)
    return {
        "format": "sceneweaver-run-comparison",
        "format_version": 1,
        "left": {"record": a, "summary": sa, "signature": signature(a)},
        "right": {"record": b, "summary": sb, "signature": signature(b)},
        "same_initial": sa["recipe_hash"] == sb["recipe_hash"]
        if sa["recipe_hash"] and sb["recipe_hash"]
        else None,
        "same_initial_model_settings": sa["initial_model_settings"] == sb["initial_model_settings"]
        if sa["initial_model_settings"] and sb["initial_model_settings"]
        else None,
        "limitations": [
            "相同初始配方不保证之后的输入、注入与操作相同，不能自动认定为严格A/B。",
            "角色ID不同，事件按各自时间顺序展示，不自动把相同序号当作相同语义步骤。",
            "评分为人工判断；没有自动生成质量总分或统计显著性结论。",
        ],
    }


def accessible(s, ids, who, lock=False):
    rows = []
    for sid in sorted(set(ids)):
        q = select(Story).where(Story.id == sid, Story.owner_id == who)
        row = s.scalar(q.with_for_update() if lock else q)
        if not row:
            raise HTTPException(404, "运行不可访问")
        rows.append(row)
    return rows


def review_data(s, row):
    payload = row.payload
    invalid = (
        privacy(s, row.left_id) != payload["left_privacy"]
        or privacy(s, row.right_id) != payload["right_privacy"]
    )
    # A human note can quote withdrawn material. Hide all authored fields after source withdrawal.
    return {
        "id": row.id,
        "left_id": row.left_id,
        "right_id": row.right_id,
        "created_at": row.created_at,
        "source_withdrawn": invalid,
        "annotation": None
        if invalid
        else {
            k: payload[k]
            for k in ("note", "left_ratings", "right_ratings", "left_evidence", "right_evidence")
        },
        "left_signature": payload["left_signature"],
        "right_signature": payload["right_signature"],
        "summaries": {"left": payload["left_summary"], "right": payload["right_summary"]},
    }


def comparison_router(db, config, owner):
    router = APIRouter(prefix="/api/role/experiments")

    def who(identity=Depends(owner)):
        if config.product != "roleplay":
            raise HTTPException(404)
        return identity

    @router.get("/compare")
    def compare(left_id: str, right_id: str, identity=Depends(who)):
        with db.session() as s:
            s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            return comparison(s, left_id, right_id, identity)

    def save_once(body, identity):
        with db.session.begin() as s:
            s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            accessible(s, [body.left_id, body.right_id], identity, lock=True)
            request_hash = digest(body.model_dump())
            lock = int(digest([identity, body.request_id])[:15], 16)
            s.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
            old = s.scalar(
                select(ExperimentReview).where(
                    ExperimentReview.owner_id == identity, ExperimentReview.request_id == body.request_id
                )
            )
            if old:
                if old.request_hash != request_hash:
                    raise HTTPException(409, "同一标注请求内容已变化")
                return review_data(s, old)
            if not body.note.strip():
                raise HTTPException(422, "请说明判断依据")
            current = comparison(s, body.left_id, body.right_id, identity)
            for side in ("left", "right"):
                if current[side]["signature"] != getattr(body, side + "_signature"):
                    raise HTTPException(409, "运行记录已变化，请刷新对照后再保存标注")
                allowed = {e["id"] for e in current[side]["record"]["events"]}
                if not set(getattr(body, side + "_evidence")).issubset(allowed):
                    raise HTTPException(422, "引用事件不属于对应运行的保留记录")
            row = ExperimentReview(
                id=uid(),
                owner_id=identity,
                left_id=body.left_id,
                right_id=body.right_id,
                request_id=body.request_id,
                request_hash=request_hash,
                payload={
                    **body.model_dump(exclude={"request_id", "left_id", "right_id"}),
                    "left_summary": current["left"]["summary"],
                    "right_summary": current["right"]["summary"],
                    "left_privacy": privacy(s, body.left_id),
                    "right_privacy": privacy(s, body.right_id),
                },
            )
            s.add(row)
            s.flush()
            return review_data(s, row)

    @router.post("/reviews", status_code=201)
    def save(body: ReviewInput, identity=Depends(who)):
        # A waiting REPEATABLE READ transaction can have an older snapshot after its lock
        # is granted. Retry the entire transaction to see a concurrent request's receipt.
        for _ in range(3):
            try:
                return save_once(body, identity)
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) not in {"40001", "23505"}:
                    raise
        raise HTTPException(409, "并发保存冲突，请重试同一标注请求")

    @router.get("/reviews")
    def reviews(left_id: str, right_id: str, identity=Depends(who)):
        with db.session() as s:
            s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            accessible(s, [left_id, right_id], identity)
            rows = list(
                s.scalars(
                    select(ExperimentReview)
                    .where(
                        ExperimentReview.owner_id == identity,
                        ExperimentReview.left_id == left_id,
                        ExperimentReview.right_id == right_id,
                    )
                    .order_by(ExperimentReview.created_at.desc(), ExperimentReview.id.desc())
                    .limit(51)
                )
            )
            return {"reviews": [review_data(s, row) for row in rows[:50]], "has_more": len(rows) > 50}

    return router
