"""Versioned, observation-scoped agreements. Call mutations inside a locked story transaction.

The event log is the durable journal. No language-model output is executable code;
consent covers explicit item IDs in exactly one proposal revision.
"""

import hashlib
import json
from types import SimpleNamespace
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .observations import append_record, audience, observed_record
from .schemas import ProposalInput
from .storage import AgreementEvent, Observation, SceneRecord, Story


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=80)
    expected_version: int = Field(ge=1)
    item_ids: list[str] = Field(min_length=1, max_length=8)
    decision: Literal["accept", "refuse", "withdraw"]


def journal(s, story_id, viewer_id=None):
    query = select(SceneRecord, AgreementEvent).join(AgreementEvent).where(SceneRecord.story_id == story_id)
    if viewer_id:
        query = query.join(Observation, Observation.event_id == SceneRecord.id).where(
            Observation.actor_id == viewer_id, Observation.perception == {}
        )
    return [
        record
        if not record.redacted
        else SimpleNamespace(
            id=record.id,
            actor_id=record.actor_id,
            payload={
                "agreement": {
                    "type": "invalidated",
                    "proposal_id": index.proposal_id,
                    "version": index.version,
                }
            },
        )
        for record, index in s.execute(query.order_by(SceneRecord.seq))
    ]


def proposals(s, story_id, viewer_id=None):
    """Never decorate an actor's old observation with unseen current consent or status."""
    result = {}
    invalid_versions = {}
    for event in journal(s, story_id, viewer_id):
        value = event.payload.get("agreement") or {}
        pid = value.get("proposal_id")
        if value.get("type") == "invalidated":
            invalid_versions[pid] = max(invalid_versions.get(pid, 0), value["version"])
            if pid in result and result[pid]["version"] <= value["version"]:
                result.pop(pid)
            continue
        if value.get("version", 0) <= invalid_versions.get(pid, -1):
            continue
        if value.get("type") == "proposal":
            result[pid] = {**value, "event_id": event.id, "decisions": {}, "completed": {}, "failures": {}}
        elif pid in result and value.get("version") == result[pid]["version"]:
            proposal = result[pid]
            if value.get("type") == "decision":
                for iid in value["item_ids"]:
                    proposal["decisions"].setdefault(iid, {})[event.actor_id] = {
                        "decision": value["decision"],
                        "event_id": event.id,
                    }
            elif value.get("type") == "completion":
                proposal["completed"][value["item_id"]] = event.id
            elif value.get("type") == "attempt_failed":
                proposal["failures"][value["item_id"]] = event.id
    for proposal in result.values():
        proposal["item_states"] = {}
        for item in proposal["items"]:
            required = {proposal["initiator_id"], item["executor_id"]}
            if item["action"].get("target_id"):
                required.add(item["action"]["target_id"])
            required.update(c["actor_id"] for c in item["conditions"] if c["kind"] == "consent")
            choices = proposal["decisions"].get(item["id"], {})
            decisions = {choices.get(aid, {}).get("decision") for aid in required}
            if item["id"] in proposal["completed"]:
                state = "success"
            elif "refuse" in decisions:
                state = "refused"
            elif "withdraw" in decisions:
                state = "cancelled"
            elif item["id"] in proposal["failures"]:
                state = "failed"
            elif decisions != {"accept"}:
                state = "wait_reply"
            elif not set(item["depends_on"]) <= proposal["completed"].keys() or not conditions_known(
                s, story_id, viewer_id, item
            ):
                state = "wait_conditions"
            else:
                state = "ready"
            proposal["item_states"][item["id"]] = {
                "state": state,
                "completion_event_id": proposal["completed"].get(item["id"]),
                "failure_event_id": proposal["failures"].get(item["id"]),
                "note": "按已观察的协议记录计算；ready仍须提交时检查现场条件。",
            }
    return list(result.values())


def conditions_known(s, story_id, viewer_id, item):
    """Read only this viewer's evidence. Unknown prerequisites stay unresolved."""
    from .observations import perceived_cast
    from .scene_state import objects
    from .storage import Conversation

    for condition in item["conditions"]:
        kind = condition["kind"]
        if kind == "consent":
            continue  # Checked with the required parties before this helper.
        if not viewer_id:
            return False
        if kind == "event" and not observed_record(s, viewer_id, condition["event_id"]):
            return False
        if kind == "tick" and s.get(Story, story_id).settings.get("world_tick", 0) < condition["tick"]:
            return False
        if kind == "custody":
            obj = next(
                (obj for obj in objects(s, story_id, viewer_id) if obj["id"] == item["action"]["object_id"]),
                {},
            )
            if obj.get("holder_id") != condition["actor_id"]:
                return False
        if kind == "co_location" and condition["actor_id"] not in {
            person["id"] for person in perceived_cast(s, s.get(Conversation, viewer_id))
        }:
            return False
    return True


def get_proposal(s, actor, pid, *, global_view=False):
    value = next(
        (
            p
            for p in proposals(s, actor.story_id, None if global_view else actor.id)
            if p["proposal_id"] == pid
        ),
        None,
    )
    if value is None:
        raise ValueError("提案不可用")
    return value


def replay(s, actor, request_id, body):
    token = hashlib.sha256(request_id.encode()).hexdigest()
    dedupe = f"agreement:{actor.id}:{token}"
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    old = s.scalar(
        select(SceneRecord).where(SceneRecord.story_id == actor.story_id, SceneRecord.dedupe == dedupe)
    )
    if old and (old.redacted or old.payload.get("command_hash") != digest):
        raise ValueError("操作标识已用于其他内容或已删除")
    return old, dedupe, digest


def propose(s, actor, body: ProposalInput):
    old, dedupe, digest = replay(s, actor, body.request_id, body.model_dump())
    if old:
        return old
    text, observers, parent, metadata = prepare_proposal(s, actor, body, dedupe)
    event = append_record(
        s,
        story_id=actor.story_id,
        actor=actor,
        kind="speech",
        text=text,
        observer_ids=observers,
        channel="private",
        addressees=[aid for aid in observers if aid != actor.id],
        dedupe=dedupe,
        parent_id=parent,
        payload={"command_hash": digest, "agreement": metadata},
    )
    return event


def prepare_proposal(s, actor, body: ProposalInput, operation_id):
    source = observed_record(s, actor.id, body.source_event_id)
    if not source or source.kind != "speech":
        raise ValueError("提案必须关联角色已经观察到的发言")
    pid = body.proposal_id or str(uuid5(NAMESPACE_URL, operation_id))
    if body.proposal_id:
        previous = get_proposal(s, actor, pid, global_view=True)
        if previous["initiator_id"] != actor.id or previous["version"] != body.expected_version:
            raise ValueError("提案版本已变化或无权修改")
        if previous["completed"]:
            raise ValueError("已执行的提案保留历史，请创建新提案")
    elif body.expected_version:
        raise ValueError("新提案版本必须为零")
    version = body.expected_version + 1
    item_ids = {item.key: str(uuid5(NAMESPACE_URL, f"{pid}:{item.key}")) for item in body.items}
    participants = {actor.id}
    items = []
    from .scene_state import objects

    known = {obj["id"]: obj for obj in objects(s, actor.story_id, actor.id)}
    for item in body.items:
        if item.action.agreement:
            raise ValueError("提案内的动作不能引用另一提案")
        if (item.action.verb == "give") != bool(item.action.target_id):
            raise ValueError("仅交接动作需要接收者")
        if item.action.object_id not in known:
            raise ValueError("不能为未知物品建立行动提案")
        participants.add(item.executor_id)
        if item.action.target_id:
            participants.add(item.action.target_id)
        for condition in item.conditions:
            if condition.actor_id:
                participants.add(condition.actor_id)
            if condition.event_id and not observed_record(s, actor.id, condition.event_id):
                raise ValueError("条件事件不在个人观察中")
        items.append(
            {
                **item.model_dump(),
                "id": item_ids[item.key],
                "depends_on": [item_ids[key] for key in item.depends_on],
            }
        )
    observers = (
        audience(s, actor, "private", sorted(participants - {actor.id}))
        if len(participants) > 1
        else [actor.id]
    )
    from .storage import Conversation

    names = {aid: s.get(Conversation, aid).current_state["profile"]["name"] for aid in participants}
    for item in items:
        action = item["action"]
        verb = {"take": "拿起", "put": "放下", "give": "交出"}[action["verb"]]
        item["description"] = names[item["executor_id"]] + verb + known[action["object_id"]]["name"]
        if action["verb"] == "give":
            item["description"] += "给" + names[action["target_id"]]
        clauses = []
        for condition in item["conditions"]:
            kind = condition["kind"]
            if kind == "co_location":
                clauses.append("确认与" + names[condition["actor_id"]] + "同场")
            elif kind == "custody":
                clauses.append("确认物品由" + names[condition["actor_id"]] + "控制")
            elif kind == "consent":
                clauses.append("获得" + names[condition["actor_id"]] + "对此项的同意")
            elif kind == "tick":
                clauses.append(f"剧情时间到达第{condition['tick']}拍")
            elif kind == "event":
                observed = observed_record(s, actor.id, condition["event_id"])
                clauses.append("观察到指定事件：“" + observed.text[:180] + "”")
        if item["depends_on"]:
            keys = {entry["id"]: entry["key"] for entry in items}
            clauses.append("先完成事项" + "、".join(keys[iid] for iid in item["depends_on"]))
        if clauses:
            item["description"] += "（条件：" + "；".join(clauses) + "）"
    # A structured proposal is an explicit new speech act, not an inferred consent.
    descriptions = [item["description"] for item in items]
    return (
        "提出行动提案：" + "；".join(descriptions),
        observers,
        source.id,
        {
            "type": "proposal",
            "proposal_id": pid,
            "version": version,
            "initiator_id": actor.id,
            "source_event_id": source.id,
            "participants": sorted(participants),
            "items": items,
        },
    )


def decide(s, actor, pid, body: DecisionInput):
    old, dedupe, digest = replay(s, actor, body.request_id, {"proposal_id": pid, **body.model_dump()})
    if old:
        return old
    text, observers, parent_id, metadata = prepare_decision(s, actor, pid, body)
    return append_record(
        s,
        story_id=actor.story_id,
        actor=actor,
        kind="speech",
        text=text,
        observer_ids=observers,
        channel="private",
        dedupe=dedupe,
        parent_id=parent_id,
        payload={"command_hash": digest, "agreement": metadata},
    )


def prepare_decision(s, actor, pid, body: DecisionInput):
    """Pure validation and exact speech rendering; commit must recheck under the story lock."""
    proposal = get_proposal(s, actor, pid)
    current = get_proposal(s, actor, pid, global_view=True)
    if current["version"] != body.expected_version or proposal["version"] != body.expected_version:
        raise ValueError("提案已变化，请重新查看")
    if actor.id not in proposal["participants"]:
        raise ValueError("不属于提案参与者")
    items = {item["id"]: item for item in proposal["items"]}
    if len(set(body.item_ids)) != len(body.item_ids) or not set(body.item_ids) <= items.keys():
        raise ValueError("同意范围必须是当前提案中的子事项")
    if set(body.item_ids) & current["completed"].keys():
        raise ValueError("已经执行的事项不能撤回或重复同意")
    observers = (
        audience(s, actor, "private", [pid for pid in proposal["participants"] if pid != actor.id])
        if len(proposal["participants"]) > 1
        else [actor.id]
    )
    verb = {"accept": "同意", "refuse": "拒绝", "withdraw": "撤回同意"}[body.decision]
    return (
        verb + "提案事项：" + "、".join(items[iid]["description"] for iid in body.item_ids),
        observers,
        proposal["event_id"],
        {
            "type": "decision",
            "proposal_id": pid,
            "version": body.expected_version,
            "item_ids": body.item_ids,
            "decision": body.decision,
        },
    )


def authorize(s, actor, command):
    """Return completion metadata. Global rules may deny, never reveal hidden reasons."""
    ref = command.get("agreement")
    if not ref:
        story = s.get(Story, actor.story_id)
        if command.get("verb") == "give" and story.settings.get("conditional_actions_version"):
            raise ValueError("交接需要指定已获同意的提案事项")
        return None
    known = get_proposal(s, actor, ref["proposal_id"])
    current = get_proposal(s, actor, ref["proposal_id"], global_view=True)
    if known["version"] != ref["version"] or current["version"] != ref["version"]:
        raise ValueError("提案不可执行，请重新确认")
    item = next((item for item in known["items"] if item["id"] == ref["item_id"]), None)
    if not item or item["executor_id"] != actor.id or item["id"] in current["completed"]:
        raise ValueError("事项不可执行")
    action = item["action"]
    if any(action[key] != command.get(key) for key in ("object_id", "verb", "target_id")):
        raise ValueError("执行动作超出提案范围")
    if not set(item["depends_on"]) <= known["completed"].keys():
        raise ValueError("前置事项尚未确认完成")
    required = {known["initiator_id"], actor.id}
    if action.get("target_id"):
        required.add(action["target_id"])
    required.update(c["actor_id"] for c in item["conditions"] if c["kind"] == "consent")
    for proposal in (known, current):
        decisions = proposal["decisions"].get(item["id"], {})
        if any(decisions.get(aid, {}).get("decision") != "accept" for aid in required):
            raise ValueError("事项尚未获得所需的明确同意")
    from .scene_state import objects

    local_objects = {obj["id"]: obj for obj in objects(s, actor.story_id, actor.id)}
    for condition in item["conditions"]:
        if condition["kind"] == "event" and not observed_record(s, actor.id, condition["event_id"]):
            raise ValueError("尚未观察到所需事件")
        if (
            condition["kind"] == "tick"
            and s.get(Story, actor.story_id).settings.get("world_tick", 0) < condition["tick"]
        ):
            raise ValueError("尚未到达约定剧情时间")
        if condition["kind"] == "custody":
            obj = local_objects.get(action["object_id"], {})
            if obj.get("holder_id") != condition["actor_id"]:
                raise ValueError("尚未确认物品控制权")
        if condition["kind"] == "co_location":
            from .observations import perceived_cast

            if condition["actor_id"] not in {c["id"] for c in perceived_cast(s, actor)}:
                raise ValueError("尚未感知到对方同场")
    return {"type": "completion", **ref}
