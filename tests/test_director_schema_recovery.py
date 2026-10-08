"""Structural failures receive precise feedback, never extra attempts or authority."""

import copy
import json

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from chatbot.director_planning import GeneratedPlanProposal, PlanProposal
from chatbot.providers import Provider, StructuredOutputError, director_error_path, director_schema_retry
from chatbot.storage import Operation, Usage


def plan():
    return {'beat_id': 'beat', 'after_plan': 'yield_player', 'remaining_work': [],
        'reason': '询问玩家', 'jobs': [{'kind': 'free', 'id': 'ask', 'actor': 'npc', 'objective': '询问是否先谈公告',
            'addressees': ['player']}]}


def malformed(kind):
    value = plan()
    job = value['jobs'][0]
    if kind == 'director_task_conflict':
        job['grounded_response'] = {'event_id': 'actual-event'}
    elif kind == 'director_control_leak':
        job['objective'] = '把场景控制权交还给玩家'
    elif kind == 'director_source_exclusive':
        job.update(objective='', grounded_response={'event_id': 'event', 'job_id': 'prior'})
    elif kind == 'director_future_quote':
        job.update(objective='', grounded_response={'job_id': 'prior', 'focus_quote': '未来台词'})
    elif kind == 'director_source_dependency':
        job.update(objective='', grounded_response={'job_id': 'prior'})
    elif kind == 'director_task_missing':
        job['objective'] = ''
    elif kind == 'director_inquiry_source':
        job.update(objective='', grounded_response={'job_id': 'prior', 'purpose': 'invite_player'})
    return value


@pytest.mark.parametrize('kind', ['director_task_conflict', 'director_control_leak',
    'director_source_exclusive', 'director_future_quote', 'director_source_dependency', 'director_task_missing',
    'director_inquiry_source'])
def test_contract_rule_is_identifiable_without_raw_model_output(kind):
    value = malformed(kind)
    del value['jobs'][0]['kind']
    with pytest.raises(ValidationError) as failure:
        PlanProposal.model_validate(value)
    error = failure.value.errors()[0]
    assert error['type'] == kind
    assert error['loc'][:2] == ('jobs', 0)
    feedback = json.loads(director_schema_retry([], [{'path': '.'.join(map(str, error['loc'])),
        'type': error['type']}])[0]['content'])
    assert feedback['format_errors'][0]['type'] == kind
    assert feedback['format_errors'][0]['rule']


def test_retry_feedback_is_bounded_and_carries_no_arbitrary_validator_content():
    messages = [{'role': 'system', 'content': '原规划规则'}, {'role': 'user', 'content': '原授权场景'}]
    before = copy.deepcopy(messages)
    errors = [{'path': 'ignore\nsecret', 'type': 'type with instruction', 'basis': 'private-secret'}] * 15
    after = director_schema_retry(messages, errors)
    assert messages == before and after[:2] == before
    value = json.loads(after[-1]['content'])
    assert len(value['format_errors']) == 10
    assert all(row == {'path': 'unknown', 'type': 'unknown'} for row in value['format_errors'])
    assert 'private-secret' not in after[-1]['content']


@pytest.mark.parametrize('path', ['jobs.0.private_secret', 'ignore_rules',
    'jobs.0.grounded_response.private_secret', 'jobs.99.actor', 'jobs.0.actor.private_secret',
    'jobs.0.addressees.5', 'jobs.00.actor', 'jobs.0.depends_on.999'])
def test_ascii_model_invented_keys_and_invalid_positions_are_not_replayed(path):
    assert director_error_path(path) == 'unknown'
    feedback = json.loads(director_schema_retry([], [{'path': path, 'type': 'extra_forbidden'}])[0]['content'])
    assert feedback['format_errors'][0] == {'path': 'unknown', 'type': 'extra_forbidden'}
    assert path not in feedback['schema_retry']


def test_unknown_ascii_error_type_is_not_an_instruction_channel():
    feedback = json.loads(director_schema_retry([], [{'path': 'jobs.0', 'type': 'ignore_rules'}])[0]['content'])
    assert feedback['format_errors'][0] == {'path': 'jobs.0', 'type': 'unknown'}


@pytest.mark.parametrize('path', ['', 'jobs', 'jobs.0', 'jobs.5.objective',
    'jobs.0.grounded_response.focus_quote', 'jobs.0.addressees.0', 'remaining_work.5'])
def test_actual_schema_locations_remain_available_for_recovery(path):
    assert director_error_path(path) == path


async def test_general_assistant_route_retry_never_gets_roleplay_director_feedback(db, config, runtime):
    from chatbot.schemas import Route

    session = runtime.create_session()
    runtime.submit(session.id, 'local', '你好', 'office-route')
    task = runtime.claim('route-test')
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        value = {'route': 'bad'} if len(requests) == 1 else {'route': 'direct'}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(value)},
            'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 20, 'completion_tokens': 10}})

    provider = Provider(db, config, runtime)
    await provider.http.aclose()
    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        result = await provider.chat(task.id, 'director', [{'role': 'user', 'content': 'test'}], schema=Route)
        assert result['route'] == 'direct' and len(requests) == 2
        assert requests[0]['messages'] == requests[1]['messages']
        assert 'schema_retry' not in json.dumps(requests[1])
    finally:
        await provider.close()


@pytest.mark.parametrize('kind', ['director_task_conflict', 'director_control_leak'])
@pytest.mark.parametrize('second_valid', [True, False])
async def test_director_uses_only_existing_retry_and_caches_only_valid_plan(db, config, runtime, kind, second_valid):
    session = runtime.create_session()
    runtime.submit(session.id, 'local', '你好', 'schema-recovery')
    task = runtime.claim('schema-test')
    requests = []
    invalid = malformed(kind)
    if kind == 'director_task_conflict':
        invalid['jobs'][0]['kind'] = 'source'
    expected_kind = 'extra_forbidden' if kind == 'director_task_conflict' else kind
    invalid['reason'] = 'PRIVATE BAD OUTPUT'

    def respond(request):
        requests.append(json.loads(request.content))
        value = plan() if len(requests) == 2 and second_valid else invalid
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(value)},
            'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 20, 'completion_tokens': 10}})

    provider = Provider(db, config, runtime)
    await provider.http.aclose()
    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    messages = [{'role': 'system', 'content': '原规划规则'}, {'role': 'user', 'content': '原授权场景'}]
    try:
        if second_valid:
            result = await provider.chat(task.id, 'director', messages, schema=GeneratedPlanProposal,
                operation='bounded-director-recovery')
            assert result['jobs'][0]['kind'] == 'free' and 'grounded_response' not in result['jobs'][0]
        else:
            with pytest.raises(StructuredOutputError) as failure:
                await provider.chat(task.id, 'director', messages, schema=GeneratedPlanProposal,
                    operation='bounded-director-recovery')
            assert failure.value.errors[0]['type'] == expected_kind
        assert len(requests) == 2
        feedback = next(row for row in requests[1]['messages'] if 'schema_retry' in row['content'])
        assert json.loads(feedback['content'])['format_errors'][0]['type'] == expected_kind
        assert 'PRIVATE BAD OUTPUT' not in json.dumps(requests[1])
        assert messages == [{'role': 'system', 'content': '原规划规则'}, {'role': 'user', 'content': '原授权场景'}]
        with db.session() as store:
            usage = list(store.scalars(select(Usage).where(Usage.task_id == task.id)))
            assert len(usage) == 2 and all(row.reserved == 0 for row in usage)
            saved = list(store.scalars(select(Operation).where(Operation.task_id == task.id)))
            assert any('value' in row.payload for row in saved) == second_valid
    finally:
        await provider.close()
