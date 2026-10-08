"""The first formal text must not be a played acknowledgement."""

import importlib
from datetime import timedelta
from pathlib import Path

import pytest
from test_combined_actor_generation import actor_case as actor_case  # noqa: F401

from chatbot.storage import Operation, SceneRecord, Task, now, uid


def test_probe_separates_ack_and_preserves_actual_formal_intent(actor_case, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'scripts'))
    module = importlib.import_module('probe_beat_reuse')
    case, start = actor_case, now()
    ack_id, text_id = uid(), uid()
    with case.services.db.session.begin() as s:
        task = s.get(Task, case.task.id)
        task.result = {'segments': [{'event_id': text_id, 'intent': {'addressee_id': case.turn.player_id}}]}
        s.add(Operation(id='probe-timing', task_id=task.id, payload={'timing_v1': {'started_at': start.isoformat()}}))
        for index, (event_id, text, ack) in enumerate([(ack_id, '嗯。', True), (text_id, '你好。', False)]):
            s.add(SceneRecord(id=event_id, story_id=case.turn.story_id, seq=100 + index,
                task_id=task.id, kind='speech', actor_id=case.actor.id, actor_name='馆长',
                text=text, dedupe='probe-' + event_id, addressees=[case.turn.player_id],
                payload={'acknowledgement': True} if ack else {},
                created_at=start + timedelta(seconds=index + 1)))
    observed = module.report(case.services.db, case.task, 3)
    assert observed['first_committed_text_s'] == 2
    assert [row['event_id'] for row in observed['segments']] == [text_id]
    assert [row['event_id'] for row in observed['acknowledgements']] == [ack_id]
    assert observed['segments'][0]['intent']['addressee_id'] == case.turn.player_id


@pytest.mark.parametrize('fault', [None, 'uncommitted_source', 'wrong_source_actor', 'ack_source', 'wrong_dependency'])
def test_probe_records_committed_npc_link_and_rejects_lookalikes(actor_case, monkeypatch, fault):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'scripts'))
    module = importlib.import_module('probe_beat_reuse')
    case, start = actor_case, now()
    source_id, reply_id = uid(), uid()
    source_actor, reply_actor = case.turn.actor_ids[:2]
    proof = {'assignment_authorization': 'grounded_response', 'director_revision': 1,
        'job_id': 'reply', 'actor_id': reply_actor, 'source_event_id': source_id,
        'source_job_id': 'source', 'source_candidate_revision': 1, 'source_producer_actor': source_actor}
    if fault == 'wrong_source_actor':
        proof['source_producer_actor'] = reply_actor
    ledger = {'revision': 1, 'plan': {'jobs': [{'id': 'reply', 'actor': reply_actor,
        'depends_on': [] if fault == 'wrong_dependency' else ['source']}]}, 'candidates': {
        'source': {'status': 'committed', 'event_id': source_id, 'revision': 1},
        'reply': {'status': 'committed', 'event_id': reply_id, 'revision': 1}}}
    with case.services.db.session.begin() as s:
        task = s.get(Task, case.task.id)
        task.result = {'segments': [{'event_id': reply_id}]}
        s.add(Operation(id=task.id + ':director-ledger', task_id=task.id, payload=ledger))
        s.add(Operation(id=task.id + ':assignment-grounded-v1:test', task_id=task.id, payload=proof))
        for index, (event_id, actor, text) in enumerate([(source_id, source_actor, '你怎么看？'),
                (reply_id, reply_actor, '我还不确定。')]):
            s.add(SceneRecord(id=event_id, story_id=case.turn.story_id, seq=100 + index,
                task_id=task.id, kind='speech', actor_id=actor, actor_name='NPC', text=text,
                dedupe='source-proof-' + event_id, addressees=[source_actor if index else reply_actor],
                payload={'acknowledgement': True} if fault == 'ack_source' and index == 0 else {},
                created_at=start + timedelta(seconds=index + 1)))
            if index or fault != 'uncommitted_source':
                s.add(Operation(id=task.id + f':committed:{index}', task_id=task.id,
                    payload={'event_id': event_id, 'actor_id': actor}))
    observed = module.report(case.services.db, case.task, 3)
    audit = observed['source_authorizations'][0]
    assert audit['source_text'] == '你怎么看？'
    assert audit['response_text'] == '我还不确定。'
    assert audit['verified_committed_link'] == (fault is None)
    assert audit['semantic_relevance'] == 'not assessed by structural evidence'
