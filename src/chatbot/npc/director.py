"""Build director input from a frozen scene and shared, actually perceived events."""

import json
from dataclasses import dataclass, replace

from sqlalchemy import select

from ..directing import DirectorPlan, SceneDirection
from ..director_planning import propose_plan
from ..director_state import ack_only_world_advance, install_plan
from ..observability.timing import annotate
from ..observations import perceived_cast, visible_records
from ..runtime import StaleTask
from ..storage import Conversation, ExperimentInitial, Operation, Story
from .director_context import bounded_public_context, common_events, event_view, reply_links
from .exchange_requirement import npc_exchange_observed


@dataclass(frozen=True)
class DirectionSnapshot:
    story_id: str
    world_seq: int
    node_id: str
    entry_event_id: str
    direction: SceneDirection
    beat_id: str
    eligible: set[str]
    visible_to: dict[str, set[str]]
    public_context: str
    finished: bool = False
    current_player_event_id: str | None = None


def pending_recovery_actors(ledger, eligible, limit):
    """Keep scheduled speakers whose speech was not committed before recovery."""
    if not ledger or limit < 1:
        return []
    pending = []
    candidates = ledger.get("candidates", {})
    for job in ledger.get("plan", {}).get("jobs", []):
        actor = job["actor"]
        if (
            actor in eligible
            and actor not in pending
            and candidates.get(job["id"], {}).get("status") != "committed"
        ):
            pending.append(actor)
    return pending[:limit]


def snapshot(s, task, turn, *, beat_id=None):
    story = s.get(Story, turn.story_id)
    origin = s.get(ExperimentInitial, turn.story_id)
    if not story or story.owner_id != task.owner_id:
        raise StaleTask("故事不可访问。")
    if not origin:
        return None
    state = story.settings.get("narrative", {})
    node = next(
        (n for n in (origin.recipe.get("narrative") or {}).get("nodes", []) if n["id"] == state.get("node")),
        None,
    )
    if not node or not node.get("direction"):
        return None
    direction = SceneDirection.model_validate(node["direction"])
    mapping = origin.receipt["actors"]
    for beat in direction.beats:
        try:
            beat.role_focus = {mapping[alias]: focus for alias, focus in beat.role_focus.items()}
            beat.participants = [mapping[a] for a in beat.participants]
            if beat.direct_response_actor:
                beat.direct_response_actor = mapping[beat.direct_response_actor]
        except KeyError:
            raise ValueError("导演段落引用的角色未实例化。") from None
    progress = state.get("direction_progress", {})
    completed = (
        set(progress.get("completed", []))
        if progress.get("entry_event_id") == state.get("event_id")
        else set()
    )
    remaining = [b for b in direction.beats if b.id not in completed]
    selected = next(
        (b for b in direction.beats if b.id == (beat_id or (remaining or direction.beats)[0].id)), None
    )
    if not selected:
        raise ValueError("导演段落不存在。")
    if task.kind == "npc_step":
        selected.max_segments = 1
    player = s.get(Conversation, turn.player_id) if turn.player_id else None
    location = player.current_state.get("location", "main") if player else task.payload.get("scene")
    actors = [s.get(Conversation, aid) for aid in selected.participants if aid in turn.actor_ids]
    actors = [
        a
        for a in actors
        if a
        and a.story_id == story.id
        and not a.current_state.get("player")
        and a.current_state.get("location", "main") == location
        and a.id in task.payload.get("audience", turn.actor_ids)
        and (task.kind != "npc_step" or a.id == task.payload.get("step_actor_id"))
    ]
    cast_views = {a.id: perceived_cast(s, a) for a in actors}
    visible_to = {aid: {p["id"] for p in cast} for aid, cast in cast_views.items()}
    # Names must be explicitly bound to IDs, not inferred from quoted dialogue.
    # Only include identities shared by every participating actor's perception.
    cast = [
        person
        for person in next(iter(cast_views.values()), [])
        if all(person in view for view in cast_views.values())
    ]
    # Exact intersection avoids leaking a full quote to a partial listener.
    perceived_events = [
        [
            row
            for row in visible_records(s, a.id, last=41)
            if not (row.task_id == task.id and row.payload.get("acknowledgement"))
        ][-40:]
        for a in actors
    ]
    links = reply_links(s, {r.id for records in perceived_events for r in records})
    views = [{r.id: event_view(r, links) for r in records} for records in perceived_events]
    public_context = bounded_public_context(cast, common_events(views))
    shared = json.loads(public_context)["events"]
    # Highlight only a current input already in the common perception boundary.
    # Never copy task.payload['text']: it may contain a private/partial utterance.
    current_event = next(
        (
            row["event_id"]
            for row in shared
            if row["event_id"] == task.payload.get("event_id")
            and row["actor_id"] == turn.player_id
            and row["kind"] == "speech"
        ),
        None,
    )
    return DirectionSnapshot(
        story.id,
        story.seq,
        node["id"],
        state.get("event_id", ""),
        direction,
        selected.id,
        {a.id for a in actors},
        visible_to,
        public_context,
        not remaining,
        current_event,
    )


async def prepare_scene_plan(
    services, task, turn, *, beat_id=None, max_jobs=6, recovery_reason="", recovery_hint="", recovery_notice=None
):
    """Return None for legacy scenes; install a bounded proposal for directed ones."""
    services.runtime.check(task.id, task.lease_version)
    with services.db.session() as s:
        frozen = snapshot(s, task, turn, beat_id=beat_id)
        previous = s.get(Operation, f"{task.id}:director-ledger")
        expected_revision = previous.payload["revision"] if previous else 0
        feedback_status = "used" if recovery_notice else "absent"
        if recovery_notice and (
            recovery_reason != "assignment_rejected"
            or not recovery_notice.current(s, task, turn, previous.payload if previous else None)
        ):
            recovery_notice = None
            feedback_status = "stale"
        if recovery_reason:
            annotate(director_recovery_feedback=feedback_status)
        completed_jobs = []
        for row in s.scalars(select(Operation).where(Operation.id.startswith(f"{task.id}:director-ledger"))):
            data = row.payload
            if frozen is None or data.get("plan", {}).get("beat_id") != frozen.beat_id:
                continue
            completed_jobs.extend(data.get("inherited_jobs", []))
            for job in data.get("plan", {}).get("jobs", []):
                candidate = data.get("candidates", {}).get(job["id"], {})
                if candidate.get("status") == "committed" and candidate.get("event_id"):
                    completed_jobs.append(
                        {
                            "actor": job["actor"],
                            "objective": job["objective"],
                            "event_id": candidate["event_id"],
                        }
                    )
        completed_jobs = list({job["event_id"]: job for job in completed_jobs}.values())
        if recovery_reason and previous and previous.payload.get("recovery_count", 0) >= 1:
            raise StaleTask("本轮导演重规划次数已用完。")
    if frozen is None:
        return None
    if not 1 <= max_jobs <= 6:
        raise ValueError("导演本轮剩余片段数无效。")
    proposal_direction = frozen.direction.model_copy(deep=True)
    selected = next(b for b in proposal_direction.beats if b.id == frozen.beat_id)
    selected.max_segments = min(selected.max_segments, max_jobs)
    if task.kind == "npc_step":
        selected.npc_exchange_required = False  # A microstep is not a complete authored performance.
    if task.payload.get("game_backstage"):
        # Legacy authoring may say "handoff to player" although no player is here.
        selected.handoff = "continuous"
    if frozen.finished:
        selected.objective = "回应当前玩家的追问或行动，不重复已完成段落，不强行新增剧情。"
        selected.completion = "回应本次追问后交还玩家。"
        selected.handoff = "after_beat"
        selected.npc_exchange_required = False
    required_recovery_actors = (
        pending_recovery_actors(previous.payload if previous else None, frozen.eligible, max_jobs)
        if recovery_reason in {"assignment_rejected", "draft_rejected"}
        else []
    )
    direct = None

    def same_scope(s, *, lock_actor=False):
        if recovery_notice and not recovery_notice.installed_current(s, task, turn, lock_actor=lock_actor):
            return False
        live = snapshot(s, task, turn, beat_id=frozen.beat_id)
        if direct is not None:
            from .direct_response import compile_direct_response

            if compile_direct_response(services, task, turn, frozen, session=s) != direct:
                return False
        return live == frozen or (
            live is not None
            and replace(live, world_seq=frozen.world_seq) == frozen
            and ack_only_world_advance(s, task, turn.story_id, frozen.world_seq, live.world_seq)
        )

    def current():
        services.runtime.check(task.id, task.lease_version)
        with services.db.session() as s:
            return same_scope(s)

    from .beat_reuse import consume_frame, resume_frame

    reuse = None
    if not previous and not frozen.finished:
        reuse = await resume_frame(
            services, task, turn, frozen, max_jobs=max_jobs, recovering=bool(recovery_reason)
        )
    from .direct_response import compile_direct_response

    direct = compile_direct_response(
        services, task, turn, frozen, previous=previous, recovering=bool(recovery_reason)
    ) if reuse is None else None
    plan = (
        reuse["plan"]
        if reuse is not None
        else (
            direct if direct is not None else
            DirectorPlan(beat_id=frozen.beat_id, reason="本场互动段落已完成，交还玩家。")
            if frozen.finished and task.payload.get("autonomous")
            else await propose_plan(
                services.provider,
                task.id,
                proposal_direction,
                frozen.beat_id,
                scene_revision=f"{frozen.node_id}:{frozen.entry_event_id}:{frozen.world_seq}",
                public_context=frozen.public_context,
                current_player_event_id=frozen.current_player_event_id,
                completed_jobs=completed_jobs,
                required_recovery_actors=required_recovery_actors,
                npc_exchange_observed=npc_exchange_observed(
                    selected,
                    json.loads(frozen.public_context)["events"],
                    {job["event_id"] for job in completed_jobs},
                ),
                eligible=frozen.eligible,
                visible_to=frozen.visible_to,
                is_current=current,
                recovery_reason=recovery_reason,
                recovery_hint=recovery_hint,
                recovery_feedback=recovery_notice.notice if recovery_notice else None,
                grounded_responses=getattr(
                    getattr(services, "config", None), "role_assignment_authorization", "semantic"
                )
                == "grounded_response",
            )
        )
    )
    def on_install(s):
        if reuse:
            consume_frame(s, task, turn, frozen, reuse)
        if direct is not None:
            s.add(Operation(id=f"{task.id}:direct-response-frame", task_id=task.id,
                            payload={"plan_revision": expected_revision + 1,
                                     "actor_id": direct.jobs[0].actor, "source_id": frozen.current_player_event_id}))

    return install_plan(
        services.db,
        task,
        turn.story_id,
        frozen.world_seq,
        plan,
        expected_revision=expected_revision,
        validate_scope=lambda s: same_scope(s, lock_actor=True),
        recovery_reason=recovery_reason,
        allow_ack_advance=True,
        handoff_policy=selected.handoff,
        segment_capacity=selected.max_segments,
        inherited_jobs=reuse["inherited_jobs"] if reuse else None,
        on_install=on_install if reuse or direct is not None else None,
    )
