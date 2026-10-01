"""Canonical committed events and per-character perception. No LLM can widen an audience."""

from types import SimpleNamespace

from sqlalchemy import select

from .provenance import source_class
from .storage import Conversation, Observation, RoleMemory, SceneRecord, Story, uid


def members(s, story_id):
    return list(
        s.scalars(select(Conversation).where(Conversation.story_id == story_id).order_by(Conversation.id))
    )


def notices(observer, other):
    return observer.id == other.id or other.id not in observer.current_state.get("unnoticed_ids", [])


def perceived_cast(s, observer):
    return [
        {"id": c.id, "name": c.current_state.get("profile", {}).get("name", "")}
        for c in members(s, observer.story_id)
        if c.current_state.get("location", "main") == observer.current_state.get("location", "main")
        and notices(observer, c)
    ]


def audience(s, actor, channel="public", addressees=None):
    addressees = list(dict.fromkeys(addressees or []))
    people = members(s, actor.story_id)
    scene = actor.current_state.get("location", "main")
    present = {c.id for c in people if c.current_state.get("location", "main") == scene}
    noticed = {c.id for c in people if notices(actor, c)}
    if channel not in {"public", "private"} or any(
        sid not in present or sid not in noticed for sid in addressees
    ):
        raise ValueError("发言对象必须在当前场景，频道须为公开或私下。")
    if channel == "private" and not addressees:
        raise ValueError("私下交流必须指定接收者。")
    return sorted(present if channel == "public" else {actor.id, *addressees})


def append_record(
    s,
    *,
    story_id,
    kind,
    text,
    observer_ids,
    dedupe,
    actor=None,
    channel="public",
    addressees=None,
    parent_id=None,
    task_id=None,
    occurred_at=None,
    payload=None,
    speaker_label=None,
    perceptions=None,
):
    story = s.scalar(select(Story).where(Story.id == story_id).with_for_update())
    old = s.scalar(select(SceneRecord).where(SceneRecord.story_id == story_id, SceneRecord.dedupe == dedupe))
    if old:
        return old
    people = {c.id: c for c in members(s, story_id)}
    observers = set(observer_ids)
    if not observers.issubset(people) or (actor and actor.id not in people):
        raise ValueError("观察者不属于该剧情。")
    perceptions = perceptions or {}
    if not set(perceptions).issubset(observers):
        raise ValueError("感知内容不能扩大观察者范围。")
    for sid, perception in perceptions.items():
        heard = perception.get("text", text)
        if kind != "speech" or not heard.strip() or heard not in text:
            raise ValueError("部分听闻必须逐字来自本次发言。")
        if actor and sid == actor.id and (heard != text or not perception.get("speaker_known", True)):
            raise ValueError("发言者知道自己说过的完整内容。")
    if parent_id:
        parent = s.get(SceneRecord, parent_id)
        if not parent or parent.story_id != story_id or parent.redacted:
            raise ValueError("无效的因果来源事件。")
    if story.seq == 0:
        story.settings = {**story.settings, "request_projection_version": 1}
    if kind == "speech" and actor:
        from .presentation import freeze_speech_appearance

        payload = freeze_speech_appearance(s, story, actor, payload or {})
    story.seq += 1
    record = SceneRecord(
        id=uid(),
        story_id=story_id,
        seq=story.seq,
        kind=kind,
        actor_id=actor.id if actor else None,
        actor_name=actor.current_state.get("profile", {}).get("name", "")
        if actor
        else (speaker_label or "主持人"),
        text=text,
        channel=channel,
        scene=actor.current_state.get("location", "main") if actor else "host",
        addressees=addressees or [],
        parent_id=parent_id,
        task_id=task_id,
        dedupe=dedupe,
        occurred_at=occurred_at,
        payload=payload or {},
    )
    s.add(record)
    s.flush()
    from .event_projection import project_event

    project_event(s, record)
    for sid in sorted(observers):
        if kind in {"host", "memory_change", "relationship", "object_state"} and people[
            sid
        ].current_state.get("player"):
            people[sid].current_state = {**people[sid].current_state, "idle_issued": False}
        add_observation(s, record, sid, perceptions.get(sid))
    from .agreement_delivery import record_agreement

    record_agreement(s, record)
    return record


def perceive(record, observation):
    """Detached view: never mutate the shared ORM event when serving another observer."""
    perception = observation.perception or {}
    if not perception or record.redacted:
        return record
    heard = perception.get("text", record.text)
    known = perception.get("speaker_known", True)
    view = SimpleNamespace(**{c.name: getattr(record, c.name) for c in SceneRecord.__table__.columns})
    view.text = heard
    if not known:
        view.actor_id, view.actor_name = None, "未辨认的声音"
    if heard != record.text or not known:
        # Hidden suffixes, attribution and request IDs can also leak through metadata.
        view.payload = {"perception": {"complete": heard == record.text, "speaker_known": known}}
        if record.payload.get("presentation", {}).get("mode") == "text_fallback":
            # Delivery policy is safe to retain; never copy full-speech metadata.
            view.payload["presentation"] = {"version": 1, "mode": "text_fallback"}
        elif record.payload.get("presentation", {}).get("audio_status") == "ready":
            view.payload["presentation"] = {"version": 1, "audio_status": "ready"}
        # Only preserve audio text when the original marked this entire excerpt as speech.
        if any(heard in part for part in record.payload.get("spoken_parts", [])):
            view.payload["spoken_parts"] = [heard]
        view.addressees, view.parent_id = [], None
    return view


def add_observation(s, record, actor_id, perception=None):
    frozen = {}
    if perception:
        if perception.get("text", record.text) != record.text:
            frozen["text"] = perception["text"]
        if not perception.get("speaker_known", True):
            frozen["speaker_known"] = False
    observation = Observation(actor_id=actor_id, event_id=record.id, perception=frozen)
    s.add(observation)
    view = perceive(record, observation)
    if record.kind == "speech" and view.actor_id:
        observer = s.get(Conversation, actor_id)
        hidden = observer.current_state.get("unnoticed_ids", [])
        if view.actor_id in hidden:
            observer.current_state = {
                **observer.current_state,
                "unnoticed_ids": [aid for aid in hidden if aid != view.actor_id],
            }
            observer.revision += 1
    s.add(
        RoleMemory(
            id=uid(),
            story_id=record.story_id,
            actor_id=actor_id,
            kind="episode",
            content=f"#{record.seq} {view.actor_name}：{view.text}",
            source_ids=[record.id],
        )
    )


def observed_record(s, actor_id, event_id):
    row = s.execute(
        select(SceneRecord, Observation)
        .join(Observation)
        .where(
            SceneRecord.id == event_id,
            SceneRecord.redacted.is_(False),
            Observation.actor_id == actor_id,
        )
    ).first()
    return perceive(*row) if row else None


def visible_records(s, actor_id, *, after=0, before=None, last=None):
    q = (
        select(SceneRecord, Observation)
        .join(Observation, Observation.event_id == SceneRecord.id)
        .where(
            Observation.actor_id == actor_id,
            SceneRecord.redacted.is_(False),
            SceneRecord.seq > after,
        )
    )
    if before is not None:
        q = q.where(SceneRecord.seq <= before)
    if last is not None:
        return [
            perceive(*row)
            for row in reversed(s.execute(q.order_by(SceneRecord.seq.desc()).limit(last)).all())
        ]
    return [perceive(*row) for row in s.execute(q.order_by(SceneRecord.seq))]


def record_data(record, *, privileged=False, viewer_id=None):
    detail = {}
    if privileged:
        detail = record.payload
    elif record.kind in {"host", "authoring", "relationship", "memory_change"} or (
        viewer_id is not None and viewer_id == record.actor_id
    ):
        detail = {k: v for k, v in record.payload.items() if k != "context_source_ids"}
    elif viewer_id is not None and record.kind == "action" and not record.payload.get("perception"):
        result = record.payload.get("item_action_result") or {}
        if result.get("status") in {"completed", "rejected"}:
            # A witness can ground the visible result, without receiving the
            # actor's private command, agreement reference, or reasoning.
            detail = {"item_action_result": {"status": result["status"]}}
    return {
        "id": record.id,
        "seq": record.seq,
        "kind": record.kind,
        "source_class": source_class({"kind": record.kind, "detail": detail}),
        **(
            {"acknowledgement": True}
            if record.kind == "speech" and record.payload.get("acknowledgement")
            else {}
        ),
        "actor_id": record.actor_id,
        "speaker": record.actor_name,
        "text": record.text,
        "channel": record.channel,
        "scene": record.scene,
        "occurred_at": record.occurred_at,
        "recorded_at": record.created_at.isoformat() if record.created_at else None,
        "addressees": record.addressees,
        "parent_id": record.parent_id,
        "detail": detail,
        **(
            {"appearance": record.payload["appearance"]}
            if record.kind == "speech" and not record.redacted and record.payload.get("appearance")
            else {}
        ),
        **(
            {"performance": record.payload["performance"]}
            if record.kind == "speech" and not record.redacted and record.payload.get("performance")
            else {}
        ),
        **({"perception": record.payload["perception"]} if record.payload.get("perception") else {}),
        **({"exchange": record.payload["exchange"]} if record.payload.get("exchange") else {}),
        **(
            {"object": record.payload["object"]}
            if record.kind == "object_state" and record.payload.get("object")
            else {}
        ),
    }


def current_knowledge(actor, records):
    """Rebuild current host knowledge from scoped archive, including evicted working-set keys."""
    facts = dict(actor.current_state.get("scene_facts", {}))
    claims = dict(actor.current_state.get("scene_claims", {}))
    for event in records:
        if event.kind != "host":
            continue
        value = {**event.payload}
        # Narrative host records use the committed record itself as their source.
        # Older frozen runs may predate an explicit payload event_id.
        value.setdefault("event_id", event.id)
        key = value["key"]
        if value["basis"] in {"confirmed", "witnessed"}:
            facts.pop(key, None)
            facts[key] = value
            claims = {eid: item for eid, item in claims.items() if item["key"] != key}
        else:
            claims[value["event_id"]] = value
    return facts, claims
