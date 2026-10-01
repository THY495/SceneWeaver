from datetime import timedelta

from sqlalchemy import and_, or_, select, text

from .config import Settings
from .observability.timing import timed
from .storage import Conversation, Database, Event, Message, Story, Task, now, uid

TERMINAL = {"completed", "failed", "cancelled", "superseded"}


class StaleTask(Exception):
    pass


def publication_gate_reason(task, story, presence, tick):
    """Return why a prepared utterance cannot enter the world right now."""
    if task.kind == "npc_step":
        return None
    phase = story.settings.get("narrative", {}).get("execution", {}).get("phase")
    if story.settings.get("paused"):
        return "story_paused"
    if phase in {"opening", "video"}:
        return f"phase_{phase}"
    # A submitted player turn owns its response. Later presence updates describe
    # the next draft or reading state and must not cancel that committed turn.
    foreground_reply = task.kind == "respond" and not task.payload.get("autonomous")
    if not foreground_reply and presence.get("until", 0) > tick and presence.get("typing"):
        return "player_typing"
    if not foreground_reply and presence.get("until", 0) > tick and not presence.get("visible"):
        return "player_hidden"
    if task.payload.get("autonomous") and presence.get("until", 0) <= tick:
        return "presence_expired"
    return None


class Runtime:
    def __init__(self, db: Database, config: Settings, *, execution_scope=None):
        self.db, self.config = db, config
        self.execution_scope = execution_scope

    @staticmethod
    def emit(s, conversation, kind, payload, task_id=None):
        conversation.seq += 1
        event = Event(
            session_id=conversation.id,
            seq=conversation.seq,
            epoch=conversation.epoch,
            kind=kind,
            payload=payload,
            task_id=task_id,
        )
        s.add(event)
        return event

    def create_session(self, owner_id="local", title="新对话"):
        with self.db.session.begin() as s:
            row = Conversation(id=uid(), owner_id=owner_id, title=title)
            s.add(row)
        return row

    def enqueue(
        self,
        s,
        kind,
        owner_id,
        payload,
        *,
        session_id=None,
        epoch=0,
        priority=50,
        parent_id=None,
        dedupe=None,
    ):
        if dedupe:
            existing = s.scalar(select(Task).where(Task.dedupe_key == dedupe))
            if existing:
                return existing
        task = Task(
            id=uid(),
            kind=kind,
            owner_id=owner_id,
            payload=payload,
            session_id=session_id,
            epoch=epoch,
            priority=priority,
            parent_id=parent_id,
            dedupe_key=dedupe,
            deadline=now() + timedelta(seconds=self.config.task_timeout),
        )
        s.add(task)
        return task

    def submit(
        self,
        session_id,
        owner_id,
        content,
        client_id,
        mode="auto",
        *,
        channel="public",
        addressees=None,
        occurred_at=None,
        acknowledgement=False,
    ):
        with self.db.session.begin() as s:
            initial = s.get(Conversation, session_id)
            if initial and initial.owner_id == owner_id and initial.story_id:
                s.scalar(select(Story).where(Story.id == initial.story_id).with_for_update())
            c = s.scalar(
                select(Conversation)
                .where(Conversation.id == session_id, Conversation.owner_id == owner_id)
                .with_for_update()
            )
            if not c:
                raise KeyError("会话不存在")
            from .product_boundary import has_role_state

            if self.config.product == "office" and has_role_state(c):
                raise ValueError("会话与当前产品不匹配。")
            old = s.scalar(
                select(Message).where(Message.session_id == session_id, Message.client_id == client_id)
            )
            if old:
                return s.get(Task, old.task_id)
            from .scene_execution import opening_active, story_finished

            if c.story_id and story_finished(s.get(Story, c.story_id)):
                from fastapi import HTTPException

                raise HTTPException(409, "故事已结束，请新建运行后继续游玩。")

            if c.story_id and opening_active(s.get(Story, c.story_id)):
                from fastapi import HTTPException

                raise HTTPException(409, "开场尚未完成，请先推进至自由互动。")
            c.epoch += 1
            c.revision += 1
            # Previous output is fenced immediately; background results remain inspectable.
            for t in s.scalars(
                select(Task).where(
                    Task.session_id == session_id,
                    Task.kind == "respond",
                    Task.status.in_(["queued", "running", "waiting"]),
                )
            ):
                t.status = "superseded"
            c.current_state = {
                **c.current_state,
                "latest_user_message": content,
                "revision": c.revision,
                "last_input_at": now().timestamp(),
                "idle_issued": False,
                **(
                    {"presence": {**c.current_state["presence"], "typing": False}}
                    if "presence" in c.current_state
                    else {}
                ),
            }
            if c.title == "新对话":
                c.title = content[:40]
            task = self.enqueue(
                s,
                "respond",
                owner_id,
                {"text": content, "mode": mode, "acknowledgement": acknowledgement},
                session_id=session_id,
                epoch=c.epoch,
                priority=0,
            )
            e = self.emit(s, c, "accepted", {"text": content, "client_id": client_id}, task.id)
            message = Message(
                id=uid(),
                session_id=session_id,
                client_id=client_id,
                task_id=task.id,
                seq=e.seq,
                role="user",
                content=content,
                delivered=True,
            )
            s.add(message)
            if c.current_state.get("player"):
                from .observations import append_record, audience

                observers = audience(s, c, channel, addressees)
                # Only an explicit recipient establishes a player-to-NPC obligation.
                targets = [aid for aid in (addressees or []) if aid in observers and aid != c.id]
                record = append_record(
                    s,
                    story_id=c.story_id,
                    kind="speech",
                    text=content,
                    actor=c,
                    observer_ids=observers,
                    channel=channel,
                    addressees=addressees,
                    task_id=task.id,
                    dedupe=f"user:{client_id}",
                    occurred_at=occurred_at,
                    payload={"exchange": {"expects_reply": True, "target_id": targets[0]}}
                    if len(targets) == 1
                    else {},
                )
                task.payload = {
                    **task.payload,
                    "event_id": record.id,
                    "audience": observers,
                    "channel": channel,
                    "addressees": addressees or [],
                }
            elif c.story_id and c.current_state.get("profile"):
                from .observations import append_record

                record = append_record(
                    s,
                    story_id=c.story_id,
                    kind="speech",
                    text=content,
                    observer_ids=[c.id],
                    channel="private",
                    addressees=[c.id],
                    task_id=task.id,
                    dedupe=f"user:{c.id}:{client_id}",
                    speaker_label="用户",
                    occurred_at=occurred_at,
                )
                task.payload = {**task.payload, "event_id": record.id}
            # Roleplay uses observer-scoped events, not personal user memory.
            if not c.current_state.get("profile"):
                self.enqueue(
                    s,
                    "memory",
                    owner_id,
                    {"message_id": message.id},
                    session_id=session_id,
                    priority=80,
                    parent_id=task.id,
                    dedupe=f"memory:{message.id}",
                )
        return task

    def claim(self, worker_id, *, background=False):
        with self.db.session.begin() as s:
            eligible = or_(Task.status == "queued", and_(Task.status == "running", Task.lease_until < now()))
            q = select(Task).where(eligible, Task.deadline > now(), Task.attempt < 3)
            scope = self.execution_scope.tasks() if self.execution_scope else True
            q = q.where(scope)
            q = q.where(Task.kind != "respond") if background else q.where(Task.kind == "respond")
            if background:
                # Serialize the short background claim transaction, never a model call.
                s.execute(text("SELECT pg_advisory_xact_lock(7831001)"))
                scene_busy = s.scalar(
                    select(Task.id)
                    .where(
                        Task.kind.in_(["scene_run", "npc_step"]),
                        Task.status == "running",
                        Task.lease_until > now(),
                        scope,
                    )
                    .limit(1)
                )
                foreground_busy = s.scalar(
                    select(Task.id)
                    .where(Task.kind == "respond", Task.status.in_(["queued", "running"]), scope)
                    .limit(1)
                )
                if scene_busy or foreground_busy:
                    q = q.where(Task.kind.not_in(["scene_run", "npc_step"]))
                from sqlalchemy.orm import aliased

                parent = aliased(Task)
                foreground = select(parent.id).where(parent.status.in_(["queued", "running"]))
                q = q.where(
                    or_(
                        Task.kind != "role_reflect",
                        Task.parent_id.is_(None),
                        Task.parent_id.not_in(foreground),
                        Task.payload["urgent"].as_boolean().is_(True),
                    )
                )
            task = s.scalar(
                q.order_by(Task.priority, Task.created_at).with_for_update(skip_locked=True).limit(1)
            )
            if not task:
                return None
            task.status = "running"
            task.lease_owner = worker_id
            task.lease_version += 1
            task.attempt += 1
            task.lease_until = now() + timedelta(seconds=self.config.lease_seconds)
            task.updated_at = now()
        return task

    def check(self, task_id, lease_version=None):
        with self.db.session() as s:
            task = s.get(Task, task_id)
            if self.execution_scope and not s.scalar(
                select(Task.id).where(Task.id == task_id, self.execution_scope.tasks())
            ):
                raise StaleTask("任务不属于当前发行作品。")
            if not task or task.status != "running" or task.deadline < now():
                raise StaleTask("任务已停止或超时")
            if (
                not task.lease_until
                or task.lease_until < now()
                or (lease_version is not None and task.lease_version != lease_version)
            ):
                raise StaleTask("任务租约已失效")
            if task.kind == "respond":
                c = s.get(Conversation, task.session_id)
                if c.epoch != task.epoch:
                    raise StaleTask("输入已更新")
            if task.kind in {"scene_run", "npc_step"}:
                from .scene_tasks import valid_scene

                if not valid_scene(s, task, s.get(Story, task.payload["story_id"])):
                    raise StaleTask("场景活动已停止或参与者发生变化")
            return task

    def heartbeat(self, task_id, version):
        with self.db.session.begin() as s:
            t = s.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if not t or t.status != "running" or t.lease_version != version or t.lease_until < now():
                return False
            t.lease_until = now() + timedelta(seconds=self.config.lease_seconds)
            return True

    def publish(self, task_id, version, kind, payload):
        with self.db.session.begin() as s:
            t = s.get(Task, task_id)
            if not t or not t.session_id:
                return
            c = s.scalar(select(Conversation).where(Conversation.id == t.session_id).with_for_update())
            s.refresh(t)
            if (
                t.status != "running"
                or t.lease_version != version
                or t.lease_until < now()
                or c.epoch != t.epoch
            ):
                raise StaleTask("过时结果已隔离")
            self.emit(s, c, kind, payload, t.id)

    @timed("task.finish")
    def finish(self, task_id, version, result=None, error=None):
        with self.db.session.begin() as s:
            t = s.get(Task, task_id)
            if not t:
                return False
            parent = s.get(Conversation, t.session_id) if t.session_id else None
            step_story = None
            if t.kind == "npc_step":
                step_story = s.scalar(
                    select(Story).where(Story.id == t.payload["story_id"]).with_for_update()
                )
            if parent and parent.story_id:
                s.scalar(select(Story).where(Story.id == parent.story_id).with_for_update())
            if result and result.get("actor_revisions"):
                parent = s.get(Conversation, t.session_id)
                s.scalar(select(Story).where(Story.id == parent.story_id).with_for_update())
                members = list(
                    s.scalars(
                        select(Conversation)
                        .where(
                            Conversation.story_id == parent.story_id,
                        )
                        .order_by(Conversation.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                )
                versions = {m.id: m.revision for m in members}
                if any(versions.get(sid) != revision for sid, revision in result["actor_revisions"].items()):
                    error, result = "角色状态已变化，请重新发言。", None
            c = (
                s.scalar(
                    select(Conversation)
                    .where(Conversation.id == t.session_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if t.session_id
                else None
            )
            t = s.scalar(select(Task).where(Task.id == task_id).with_for_update())
            s.refresh(t)
            if t.status != "running" or t.lease_version != version or t.lease_until < now():
                return False
            if t.kind == "npc_step" and not error:
                from .experiments import valid_step

                if not valid_step(s, t, step_story):
                    error, result = "实验许可已失效，未提交决策结果。", None
            if t.kind == "respond" and c.epoch != t.epoch:
                t.status = "superseded"
                return False
            t.status = "failed" if error else "completed"
            t.error, t.result = error, result or (t.result if error and t.result.get("incremental") else {})
            t.updated_at = now()
            if c and t.kind == "respond":
                if not error and not t.result.get("incremental"):
                    if c.story_id and c.current_state.get("profile") and not c.current_state.get("player"):
                        from .observations import append_record, visible_records

                        append_record(
                            s,
                            story_id=c.story_id,
                            kind="speech",
                            actor=c,
                            text=t.result.get("answer", ""),
                            observer_ids=[c.id],
                            channel="private",
                            task_id=t.id,
                            parent_id=t.payload.get("event_id"),
                            dedupe=f"task:{t.id}:solo",
                            payload={
                                "context_source_ids": [
                                    r.id for r in visible_records(s, c.id) if r.task_id != t.id
                                ]
                            },
                        )
                    if c.current_state.get("player") and t.result.get("segments"):
                        from .observations import append_record

                        for index, segment in enumerate(t.result["segments"]):
                            actor = s.get(Conversation, segment["actor_id"])
                            append_record(
                                s,
                                story_id=c.story_id,
                                kind="speech",
                                actor=actor,
                                text=segment["text"],
                                observer_ids=t.payload.get("audience", [c.id, actor.id]),
                                dedupe=f"task:{t.id}:{index}",
                                task_id=t.id,
                                channel=t.payload.get("channel", "public"),
                                addressees=[c.id],
                                parent_id=t.payload.get("event_id"),
                            )
                    e = self.emit(
                        s,
                        c,
                        "segment_committed",
                        {
                            "text": t.result.get("answer", ""),
                            "evidence": t.result.get("evidence", []),
                            "route": t.result.get("route", "direct"),
                        },
                        t.id,
                    )
                    previous = s.scalar(
                        select(Message).where(Message.task_id == t.id, Message.role == "assistant")
                    )
                    if not previous:
                        s.add(
                            Message(
                                session_id=c.id,
                                task_id=t.id,
                                seq=e.seq,
                                role="assistant",
                                content=t.result.get("answer", ""),
                            )
                        )
                self.emit(s, c, "failed" if error else "completed", {"error": error}, t.id)
            return True

    @timed("world.commit")
    def commit_role_segment(
        self,
        task,
        actor_id,
        text,
        *,
        step,
        revision,
        intent,
        evidence=None,
        claims=None,
        spoken_parts=None,
        performance=None,
        director_candidate=None,
        prepared_audio=None,
        prepared_audio_status=None,
    ):
        """Commit one speech/action atomically. Retries cannot replay an already visible segment."""
        from .observations import append_record, visible_records
        from .storage import Operation, SceneRecord

        with self.db.session.begin() as s:
            scene_task = task.kind in {"scene_run", "npc_step"}
            parent = s.get(Conversation, task.session_id) if task.session_id else None
            story = s.scalar(
                select(Story)
                .where(Story.id == (task.payload["story_id"] if scene_task else parent.story_id))
                .with_for_update()
            )
            people = list(
                s.scalars(
                    select(Conversation)
                    .where(Conversation.story_id == story.id)
                    .order_by(Conversation.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            by_id = {c.id: c for c in people}
            c, actor = by_id[parent.id] if parent else None, by_id.get(actor_id)
            current = s.get(Task, task.id)
            old = s.scalar(
                select(SceneRecord).where(
                    SceneRecord.story_id == story.id, SceneRecord.dedupe == f"task:{task.id}:{step}"
                )
            )
            director_publication = None
            if director_candidate is not None:
                from .director_state import check_publication

                director_publication = check_publication(
                    s,
                    task,
                    story,
                    director_candidate,
                    step=step,
                    actor_id=actor_id,
                    value={
                        "text": text,
                        "revision": revision,
                        "intent": intent,
                        "evidence": evidence or [],
                        "claims": claims or [],
                        "spoken_parts": spoken_parts or [],
                        "performance": performance or [],
                    },
                    event_id=old.id if old else None,
                )
            elif s.get(Operation, f"{task.id}:director-ledger") is not None:
                raise StaleTask("导演演出必须绑定已审查候选。")
            if old:
                return old.id
            if (
                current.status != "running"
                or current.deadline <= now()
                or current.lease_version != task.lease_version
                or not current.lease_until
                or current.lease_until <= now()
                or (c is not None and c.epoch != task.epoch)
                or not actor
                or actor.revision != revision
            ):
                raise StaleTask("角色或回合已变化，旧片段未提交。")
            if scene_task:
                from .scene_tasks import valid_scene

                if not valid_scene(s, current, story):
                    raise StaleTask("场景活动已停止或参与者发生变化")
            presence = (
                c.current_state.get("presence", {})
                if c
                else {"until": story.settings.get("activity_until", 0), "visible": True}
            )
            gate = publication_gate_reason(task, story, presence, now().timestamp())
            if gate:
                # A prepared candidate can be discarded without a world event.
                # Persist only the reason, never its unapproved text or audio.
                gate_id = f"{task.id}:publication-gate:{step}"
                if not s.get(Operation, gate_id):
                    s.add(Operation(id=gate_id, task_id=task.id, payload={"reason": gate}))
                return None
            allowed = task.payload.get("audience", [c.id, actor.id] if c else [])
            if task.kind == "npc_step" and (step != 0 or actor_id != task.payload["step_actor_id"]):
                raise StaleTask("单步许可不允许额外角色或片段。")
            if actor.id not in allowed:
                raise StaleTask("角色不在本次交流范围。")
            preceding = (
                s.scalar(
                    select(SceneRecord).where(
                        SceneRecord.story_id == story.id, SceneRecord.dedupe == f"task:{task.id}:{step - 1}"
                    )
                )
                if step
                else None
            )
            from .event_projection import requests_for
            from .scene_state import transition

            response_to = intent.get("reply_to")
            if response_to:
                pending = {r["question_id"]: r for r in requests_for(s, actor.id)}
                question = pending.get(response_to)
                if (
                    not question
                    or question["target_id"] != actor.id
                    or question["status"] not in {"open", "partial", "defer"}
                ):
                    raise StaleTask("待回应事项已变化或不属于此角色。")
            object_state = None
            item_result = None
            if intent.get("act") == "item_action":
                from .items.execution import commit as commit_item
                from .items.execution import prepare, witnesses
                from .items.rules import RuleViolation

                allowed = [actor.id]
                try:
                    row, candidate, text = prepare(s, actor, intent.get("item_action") or {})
                    allowed = witnesses(s, actor, intent["item_action"])
                    commit_item(s, actor, row, candidate, f"task:{task.id}:{step}")
                    item_result = {"status": "completed", "command": intent["item_action"]}
                except RuleViolation as exc:
                    text = "尝试操作物品，但未完成。" + str(exc)
                    item_result = {"status": "rejected", "code": exc.code}
            action_attempt = None
            action_failed = False
            agreement_metadata = None
            agreement_parent = None
            if intent.get("act") == "agreement_proposal":
                from .agreements import prepare_proposal
                from .schemas import ProposalInput

                body = ProposalInput.model_validate(
                    {**(intent.get("agreement_proposal") or {}), "request_id": f"{task.id}:{step}"}
                )
                text, allowed, agreement_parent, agreement_metadata = prepare_proposal(
                    s, actor, body, f"task:{task.id}:{step}"
                )
                spoken_parts = [text]
            if intent.get("act") == "search":
                from .scene_state import search_object

                object_state, text, allowed, _ = search_object(
                    s, actor, (intent.get("search_action") or {}).get("object_id")
                )
                actor.revision += 1
            if intent.get("act") == "agreement_decision":
                from .agreements import DecisionInput, prepare_decision

                value = dict(intent.get("agreement_decision") or {})
                pid = value.pop("proposal_id", "")
                text, allowed, agreement_parent, agreement_metadata = prepare_decision(
                    s, actor, pid, DecisionInput(request_id=f"{task.id}:{step}", **value)
                )
                spoken_parts = [text]
            if intent.get("act") == "object_action":
                from .action_attempts import check_attempt
                from .scene_state import WorldActionError

                command = intent.get("object_action") or {}
                action_attempt = check_attempt(s, task, actor, step, command)
                try:
                    object_state, text, allowed, _ = transition(s, actor, command)
                    for person in people:
                        if person.id in allowed:
                            person.revision += 1
                except WorldActionError as exc:
                    if not action_attempt:
                        raise
                    action_failed = True
                    text, allowed = "尝试执行事项，但未完成。" + str(exc), [actor.id]
                    agreement_metadata = {"type": "attempt_failed", **command["agreement"]}
            target = None if action_failed or item_result else intent.get("addressee_id")
            from .observations import notices

            if target and (
                target not in by_id
                or not notices(actor, by_id[target])
                or by_id[target].current_state.get("location", "main")
                != actor.current_state.get("location", "main")
            ):
                raise StaleTask("目标未被此角色察觉或已离场。")
            wants_reply = bool(
                target and target != actor.id and intent.get("expects_reply") and intent.get("act") == "speak"
            )
            exchange = {
                "expects_reply": wants_reply,
                "target_id": target,
                "reply_to": response_to,
                "response_kind": "partial"
                if intent.get("remaining_request") and not intent.get("awaiting_player_action")
                else intent.get("response_kind", "answer"),
            }
            record = append_record(
                s,
                story_id=story.id,
                kind="object_state"
                if object_state
                else (
                    "action"
                    if action_failed or item_result or intent.get("act") in {"inspect", "search"}
                    else "speech"
                ),
                actor=actor,
                text=text,
                observer_ids=allowed,
                dedupe=f"task:{task.id}:{step}",
                task_id=task.id,
                channel="private"
                if agreement_metadata or item_result or intent.get("act") == "search"
                else "public"
                if object_state
                else task.payload.get("channel", "public"),
                addressees=[target] if target else [],
                parent_id=agreement_parent or (preceding.id if preceding else task.payload.get("event_id")),
                payload={
                    "object": object_state,
                    **({"item_action_result": item_result} if item_result else {}),
                    **(
                        {"object_record_role": "observation" if intent.get("act") == "search" else "mutation"}
                        if object_state
                        else {}
                    ),
                    "agreement": {"type": "completion", **intent["object_action"]["agreement"]}
                    if object_state and (intent.get("object_action") or {}).get("agreement")
                    else {"type": "completion", **intent["item_action"]["agreement"]}
                    if item_result
                    and item_result["status"] == "completed"
                    and (intent.get("item_action") or {}).get("agreement")
                    else agreement_metadata,
                    "exchange": exchange,
                    "intent": intent,
                    "claims": claims or [],
                    "spoken_parts": spoken_parts or [],
                    "performance": performance or [],
                    "evidence_ids": [e["id"] for e in evidence or []],
                    "context_source_ids": [r.id for r in visible_records(s, actor.id)],
                },
            )
            if action_attempt:
                from datetime import timedelta

                action_attempt.status = "failed" if action_failed else "completed"
                action_attempt.result_event_id = record.id
                action_attempt.retry_at = now() + timedelta(seconds=30) if action_failed else None
            if intent.get("act") == "inspect" and intent.get("goal_id"):
                from .storage import RoleMemory

                goal = s.get(RoleMemory, intent["goal_id"])
                # Only an explicitly authored lookup-only goal has an automatic completion rule.
                if (
                    goal
                    and goal.actor_id == actor.id
                    and goal.status == "active"
                    and goal.kind in {"goal", "commitment"}
                    and goal.content.startswith("查阅资料：")
                ):
                    goal.status, goal.version = "completed", goal.version + 1
                    append_record(
                        s,
                        story_id=story.id,
                        kind="memory_change",
                        text=f"已完成查阅：{goal.content}",
                        observer_ids=[actor.id],
                        channel="private",
                        task_id=task.id,
                        parent_id=record.id,
                        dedupe=f"complete:{goal.id}:{record.id}",
                        payload={"memory_id": goal.id, "status": "completed"},
                    )
            from .observations import observed_record

            if director_publication is not None:
                from .director_state import mark_publication

                audio_state = prepared_audio_status or (
                    "ready" if prepared_audio is not None else "not_requested"
                )
                if audio_state not in {"ready", "not_requested", "timeout", "failed"} or (
                    (audio_state == "ready") != (prepared_audio is not None)
                ):
                    raise StaleTask("候选语音尚未达到可发布或明确降级的状态。")
                if prepared_audio is not None:
                    from .voice_cache import bind

                    bind(s, task, record, prepared_audio)
                record.payload = {
                    **record.payload,
                    "presentation": {
                        "version": 1,
                        "release_id": record.id,
                        "audio_status": audio_state,
                        "mode": "text_fallback" if audio_state in {"timeout", "failed"} else "normal",
                    },
                }
                mark_publication(
                    *director_publication,
                    director_candidate,
                    story,
                    record.id,
                    world_action=intent.get("act") != "speak",
                )
            s.add(
                Operation(
                    id=f"{task.id}:committed:{step}",
                    task_id=task.id,
                    payload={"event_id": record.id, "actor_id": actor.id, "text": text, "intent": intent},
                )
            )
            if c is None:
                return record.id
            delivered = observed_record(s, c.id, record.id)
            if delivered is None:
                return record.id
            text = delivered.text
            if delivered.payload.get("perception"):
                evidence = []
            old_message = s.scalar(
                select(Message).where(Message.task_id == task.id, Message.role == "assistant")
            )
            label = delivered.actor_name + "：\n\n" + text
            full = (old_message.content + "\n\n" if old_message else "") + label
            current.result = {"incremental": True, "answer": full, "evidence": evidence or []}
            event = self.emit(
                s,
                c,
                "segment_committed",
                {
                    "text": full,
                    "evidence": evidence or [],
                    "route": "player",
                    "utterance_id": record.id,
                    "actor_id": delivered.actor_id,
                    "utterance_text": text,
                },
                task.id,
            )
            if old_message:
                old_message.content = full
            else:
                s.add(
                    Message(
                        id=uid(),
                        session_id=c.id,
                        task_id=task.id,
                        seq=event.seq,
                        role="assistant",
                        content=full,
                    )
                )
            return record.id

    def cancel(self, task_id, owner_id):
        with self.db.session.begin() as s:
            t = s.get(Task, task_id)
            if not t or t.owner_id != owner_id:
                raise KeyError("任务不存在")
            if t.kind == "npc_step":
                s.scalar(select(Story).where(Story.id == t.payload["story_id"]).with_for_update())
                s.refresh(t, with_for_update=True)
            story = None
            if t.kind == "respond" and t.session_id:
                actor = s.get(Conversation, t.session_id)
                if actor and actor.story_id:
                    story = s.scalar(
                        select(Story).where(Story.id == actor.story_id).with_for_update()
                    )
            c = (
                s.scalar(select(Conversation).where(Conversation.id == t.session_id).with_for_update())
                if t.session_id
                else None
            )
            if t.status in TERMINAL:
                return
            t.status = "cancelled"
            if story and story.settings.get("auto_route", {}).get("task_id") == t.id:
                story.settings = {**story.settings, "auto_route": {}}
            if c and t.kind == "respond" and c.epoch == t.epoch:
                c.epoch += 1
                self.emit(s, c, "cancelled", {}, t.id)
            for child in s.scalars(
                select(Task).where(Task.parent_id == task_id, Task.status.not_in(TERMINAL))
            ):
                child.status = "cancelled"

    def expire(self):
        with self.db.session() as s:
            rows = list(
                s.scalars(
                    select(Task).where(
                        self.execution_scope.tasks() if self.execution_scope else True,
                        Task.status.in_(["queued", "running", "waiting"]),
                        ~and_(Task.kind == "scene_run", Task.status == "waiting"),
                        or_(Task.deadline < now(), and_(Task.attempt >= 3, Task.lease_until < now())),
                    )
                )
            )
        for row in rows:
            with self.db.session.begin() as s:
                c = (
                    s.scalar(select(Conversation).where(Conversation.id == row.session_id).with_for_update())
                    if row.session_id
                    else None
                )
                t = s.scalar(select(Task).where(Task.id == row.id).with_for_update())
                if t.status in TERMINAL:
                    continue
                if t.kind == "scene_run" and t.status == "waiting":
                    continue
                if not (t.deadline < now() or (t.attempt >= 3 and t.lease_until and t.lease_until < now())):
                    continue
                t.status, t.error = "failed", "任务超过时限或重试次数，请缩小问题后重试。"
                if c and t.kind == "respond" and c.epoch == t.epoch:
                    self.emit(s, c, "failed", {"error": t.error}, t.id)
