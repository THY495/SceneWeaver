"""Director authorization and reflection scheduling must not form a lock cycle."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError
from test_combined_actor_generation import actor_case as actor_case
from test_grounded_assignment import install

from chatbot.npc.grounded_assignment import compile_response, record_authorization
from chatbot.observations import append_record
from chatbot.role_memory import RoleMemoryService
from chatbot.storage import Conversation, Operation, Story, Task


def test_director_authorization_and_reflection_schedule_have_no_lock_cycle(actor_case, db, config, runtime, fake):
    case = actor_case
    config.role_assignment_authorization = "grounded_response"
    for index in range(4):
        with db.session.begin() as session:
            append_record(session, story_id=case.turn.story_id, kind="speech",
                          actor=session.get(Conversation, case.actor.id), text=f"登记记录{index}。",
                          channel="private", observer_ids=[case.actor.id], dedupe=f"schedule-lock-{index}")
    job, _ = install(case)
    memory = RoleMemoryService(db, config, runtime, fake)
    _, info = memory.context(case.task, case.actor.id)
    assignment, proof = compile_response(case.services, case.task, case.turn, case.actor, job, info)
    author_at_actor, release_author, memory_at_lock = threading.Event(), threading.Event(), threading.Event()
    threads = {}
    memory_pid = []

    def lock_gate(conn, cursor, statement, parameters, context, many):
        if "FOR UPDATE" not in statement:
            return
        current = threading.get_ident()
        if (current == threads.get("author") and statement.lstrip().startswith("SELECT sessions.id")
            and case.actor.id in parameters.values()):
            # Actual locked_plan has already acquired Story and the parent Task.
            author_at_actor.set()
            if not release_author.wait(8):
                raise TimeoutError("Authorization actor-lock gate timed out")
        elif current == threads.get("memory") and not memory_pid:
            memory_pid.append(conn.connection.driver_connection.info.backend_pid)
            memory_at_lock.set()

    def authorize():
        threads["author"] = threading.get_ident()
        return record_authorization(case.services, case.task, case.actor, job, assignment, proof, "schedule-race:")

    def schedule():
        threads["memory"] = threading.get_ident()
        return memory.schedule(case.actor.id, config.workspace_owner, parent_id=case.task.id, force=True)

    event.listen(db.engine, "before_cursor_execute", lock_gate)
    outcomes, errors = {}, {}
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            authorization = pool.submit(authorize)
            try:
                reached = author_at_actor.wait(5)
                if not reached and authorization.done():
                    authorization.result()
                assert reached, "Authorization did not reach its actual actor lock"
                scheduling = pool.submit(schedule)
                assert memory_at_lock.wait(5)
                # Observe the live backend waiting on a real row lock, rather
                # than inferring that the intended interleaving happened.
                limit = time.monotonic() + 3
                waiting = False
                while time.monotonic() < limit:
                    with db.session() as session:
                        waiting = session.scalar(text(
                            "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"),
                            {"pid": memory_pid[0]})
                    if waiting:
                        break
                    time.sleep(0.01)
                assert waiting, "Scheduler never waited on the director's real lock"
            finally:
                release_author.set()
            for name, future in (("authorization", authorization), ("scheduling", scheduling)):
                try:
                    outcomes[name] = future.result(timeout=5)
                except OperationalError as exc:
                    errors[name] = {"type": type(exc.orig).__name__, "sqlstate": getattr(exc.orig, "sqlstate", None)}
        assert not errors, {"database_errors": errors, "finished": sorted(outcomes)}
        assert outcomes["scheduling"] is not None
        with db.session() as session:
            reflections = list(session.scalars(select(Task).where(
                Task.parent_id == case.task.id, Task.kind == "role_reflect")))
            assert len(reflections) == 1 and reflections[0].id == outcomes["scheduling"].id
            ledger = session.get(Operation, f"{case.task.id}:director-ledger")
            assert ledger.payload["authorizations"][job.id]["actor_id"] == case.actor.id
    finally:
        release_author.set()
        event.remove(db.engine, "before_cursor_execute", lock_gate)


def test_explicit_reflection_keeps_pause_permission_and_request_deduplication(actor_case, db, config, runtime, fake):
    case = actor_case
    for index in range(4):
        with db.session.begin() as session:
            append_record(session, story_id=case.turn.story_id, kind="speech",
                          actor=session.get(Conversation, case.actor.id), text=f"登记记录{index}。",
                          channel="private", observer_ids=[case.actor.id], dedupe=f"pause-schedule-{index}")
    memory = RoleMemoryService(db, config, runtime, fake)
    with pytest.raises(ValueError, match="暂停"):
        memory.schedule(case.actor.id, config.workspace_owner, force=True, story_id=case.turn.story_id,
                        request_id="explicit-reflection")
    with pytest.raises(KeyError, match="角色不存在"):
        memory.schedule(case.actor.id, "another-owner", force=True)
    with db.session.begin() as session:
        story = session.get(Story, case.turn.story_id)
        story.settings = {**story.settings, "paused": True}
    first = memory.schedule(case.actor.id, config.workspace_owner, force=True, story_id=case.turn.story_id,
                            request_id="explicit-reflection")
    second = memory.schedule(case.actor.id, config.workspace_owner, force=True, story_id=case.turn.story_id,
                             request_id="explicit-reflection")
    assert first.id == second.id
    assert first.payload["story_id"] == case.turn.story_id
    with db.session() as session:
        reflections = list(session.scalars(select(Task).where(Task.kind == "role_reflect")))
        assert len(reflections) == 1


@pytest.mark.parametrize("change", ["owner", "story"])
def test_automatic_schedule_reloads_actor_after_lock_wait(actor_case, db, config, runtime, fake, change):
    case = actor_case
    with db.session.begin() as session:
        other = Story(owner_id=config.workspace_owner, title="另一故事")
        session.add(other)
        session.flush()
        other_id = other.id
    memory = RoleMemoryService(db, config, runtime, fake)
    before_story, release = threading.Event(), threading.Event()
    scheduling_thread = []

    def hold_before_story(conn, cursor, statement, parameters, context, many):
        if (scheduling_thread and threading.get_ident() == scheduling_thread[0]
            and statement.lstrip().startswith("SELECT stories.id") and "FOR UPDATE" in statement):
            before_story.set()
            if not release.wait(5):
                raise TimeoutError("Schedule ownership recheck gate timed out")

    def schedule():
        scheduling_thread.append(threading.get_ident())
        return memory.schedule(case.actor.id, config.workspace_owner, force=True)

    event.listen(db.engine, "before_cursor_execute", hold_before_story)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            scheduling = pool.submit(schedule)
            try:
                assert before_story.wait(5)
                with db.session.begin() as session:
                    actor = session.get(Conversation, case.actor.id)
                    if change == "owner":
                        actor.owner_id = "another-owner"
                    else:
                        actor.story_id = other_id
            finally:
                release.set()
            with pytest.raises(KeyError):
                scheduling.result(timeout=5)
        with db.session() as session:
            assert session.scalar(select(Task.id).where(Task.kind == "role_reflect")) is None
    finally:
        release.set()
        event.remove(db.engine, "before_cursor_execute", hold_before_story)
