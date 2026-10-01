"""Completion must refresh the parent cached before acquiring its lock."""

import pytest
from sqlalchemy import event, select

from chatbot.storage import Conversation, Event, Message, Task


@pytest.mark.parametrize("new_input", [False, True])
def test_finish_refreshes_parent_after_competing_commit(db, runtime, new_input):
    conversation = runtime.create_session()
    runtime.submit(conversation.id, "local", "你好", "first")
    task = runtime.claim("completion-race")
    injected = False

    def competing_commit(session, instance):
        nonlocal injected
        if injected or not isinstance(instance, Conversation) or instance.id != conversation.id:
            return
        injected = True
        # finish has read its parent but has not yet locked it. Commit exactly
        # here, rather than relying on a scheduler to reproduce the race.
        with db.session.begin() as other:
            current = other.scalar(
                select(Conversation).where(Conversation.id == instance.id).with_for_update()
            )
            if new_input:
                current.epoch += 1
            runtime.emit(other, current, "competing_notice", {})

    event.listen(db.session, "loaded_as_persistent", competing_commit)
    try:
        completed = runtime.finish(task.id, task.lease_version, {"answer": "已完成"})
    finally:
        event.remove(db.session, "loaded_as_persistent", competing_commit)
    assert injected and completed is (not new_input)
    with db.session() as session:
        status = session.get(Task, task.id).status
        assert status == ("superseded" if new_input else "completed")
        answers = session.scalars(
            select(Message).where(Message.task_id == task.id, Message.role == "assistant")
        ).all()
        assert len(answers) == (0 if new_input else 1)
        records = session.scalars(
            select(Event).where(Event.session_id == conversation.id).order_by(Event.seq)
        ).all()
        assert len({record.seq for record in records}) == len(records)
        assert session.get(Conversation, conversation.id).seq == records[-1].seq
