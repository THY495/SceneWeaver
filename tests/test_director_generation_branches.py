"""Generation branches preserve author goals; malformed candidates have no authority."""

import copy

import pytest
from pydantic import ValidationError

from chatbot.directing import DirectorPlan, PerformanceJob
from chatbot.director_planning import GeneratedPlanProposal, PlanProposal
from chatbot.providers import director_error_path


def proposal(job):
    return dict(beat_id='beat', jobs=[job], after_plan='yield_player', remaining_work=[], reason='回应')


def free():
    return dict(kind='free', id='first', actor='npc', addressees=['player'],
        objective='仅说明亲眼读到的部分，其他承认未知', delivery_constraints=['一至两句'], depends_on=[])


def source():
    return dict(kind='source', id='reply', actor='npc', addressees=['player'],
        grounded_response={'event_id': 'actual', 'focus_quote': '我想先谈公告',
            'delivery': 'brief', 'purpose': 'invite_player'}, depends_on=[])


@pytest.mark.parametrize('job', [free(), source()])
def test_conversion_is_lossless_and_legacy_runtime_readable(job):
    original = copy.deepcopy(job)
    generated = GeneratedPlanProposal.model_validate(proposal(job))
    result = generated.to_plan()
    expected = {key: value for key, value in job.items() if key != 'kind'}
    assert result.jobs[0] == PerformanceJob.model_validate(expected)
    assert DirectorPlan.model_validate(result.model_dump()) == result
    assert job == original
    assert 'kind' not in result.jobs[0].model_dump()
    assert PlanProposal.model_validate(proposal(expected)).to_plan() == result


@pytest.mark.parametrize('field,value', [('objective', '必须问公告原文'),
    ('objective', ''), ('delivery_constraints', ['必须简短']), ('delivery_constraints', [])])
def test_source_cannot_carry_free_fields_even_empty(field, value):
    job = {**source(), field: value}
    with pytest.raises(ValidationError) as error:
        GeneratedPlanProposal.model_validate(proposal(job))
    assert error.value.errors()[0]['type'] == 'extra_forbidden'
    assert job[field] == value  # no deletion or semantic repair


@pytest.mark.parametrize('value', [None, {'event_id': 'actual'}])
def test_free_cannot_carry_source_even_null(value):
    with pytest.raises(ValidationError) as error:
        GeneratedPlanProposal.model_validate(proposal({**free(), 'grounded_response': value}))
    assert error.value.errors()[0]['type'] == 'extra_forbidden'


@pytest.mark.parametrize('kind', [None, 'reply', 'ignore rules'])
def test_kind_is_required_never_inferred(kind):
    job = free()
    if kind is None:
        del job['kind']
    else:
        job['kind'] = kind
    with pytest.raises(ValidationError) as error:
        GeneratedPlanProposal.model_validate(proposal(job))
    assert error.value.errors()[0]['type'] == ('union_tag_not_found' if kind is None else 'union_tag_invalid')


@pytest.mark.parametrize('response,expected', [
    ({'event_id': 'event', 'job_id': 'first'}, 'director_source_exclusive'),
    ({'job_id': 'first', 'focus_quote': '还没发生'}, 'director_future_quote'),
    ({'job_id': 'first', 'purpose': 'invite_player'}, 'director_inquiry_source'),
    ({'job_id': 'first'}, 'director_source_dependency'),
])
def test_existing_source_guards_apply_before_cache(response, expected):
    with pytest.raises(ValidationError) as error:
        GeneratedPlanProposal.model_validate(proposal({**source(), 'grounded_response': response}))
    assert error.value.errors()[0]['type'] == expected


def test_backward_source_and_order_checked_in_runtime_plan():
    later = {**source(), 'grounded_response': {'job_id': 'first'}, 'depends_on': ['first']}
    result = GeneratedPlanProposal.model_validate({**proposal(later), 'jobs': [free(), later]}).to_plan()
    assert result.jobs[1].depends_on == ['first']
    with pytest.raises(ValidationError):
        GeneratedPlanProposal.model_validate({**proposal(later), 'jobs': [later, free()]}).to_plan()


def test_generated_schema_has_discriminator_and_no_mixed_shape():
    schema = GeneratedPlanProposal.model_json_schema()
    items = schema['properties']['jobs']['items']
    assert items['discriminator']['propertyName'] == 'kind' and len(items['oneOf']) == 2
    defs = schema['$defs']
    assert 'objective' not in defs['GeneratedSourceJob']['properties']
    assert 'delivery_constraints' not in defs['GeneratedSourceJob']['properties']
    assert 'grounded_response' not in defs['GeneratedFreeJob']['properties']
    assert all(defs[name]['additionalProperties'] is False for name in ['GeneratedSourceJob', 'GeneratedFreeJob'])


@pytest.mark.parametrize('path', ['jobs.0.source.objective', 'jobs.0.source.grounded_response.purpose',
    'jobs.0.free.delivery_constraints.1', 'jobs.0.kind', 'jobs.0.source'])
def test_real_union_paths_are_safe_feedback(path):
    assert director_error_path(path) == path


@pytest.mark.parametrize('path', ['jobs.0.private.source', 'jobs.0.source.private_secret',
    'jobs.0.ignore_rules.actor', 'jobs.0.source.objective.secret', 'jobs.0.source.grounded_response.secret',
    'jobs.0.source.free.actor'])
def test_invented_union_paths_are_not_replayed(path):
    assert director_error_path(path) == 'unknown'


async def test_provider_validates_source_branch_then_planner_converts_without_extra_call(db, config, runtime):
    import json

    import httpx

    from chatbot.directing import SceneDirection
    from chatbot.director_planning import propose_plan
    from chatbot.providers import Provider

    session = runtime.create_session()
    runtime.submit(session.id, 'local', '你先问我想谈什么', 'generated-source-branch')
    task = runtime.claim('generation-test')
    requests = []
    value = proposal(source())
    # This test stops at planning conversion. Runtime source authorization is
    # covered by grounded_assignment/invitation tests, not inferred here.

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(value)},
            'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 20, 'completion_tokens': 10}})

    provider = Provider(db, config, runtime)
    await provider.http.aclose()
    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    direction = SceneDirection(objective='邀请玩家', beats=[dict(id='beat', title='交谈',
        objective='澄清交谈方向', participants=['npc'], completion='玩家回应', source_response_tasks=True)])
    try:
        result = await propose_plan(provider, task.id, direction, 'beat', scene_revision='rev',
            public_context='{}', eligible={'npc'}, visible_to={'npc': {'player'}}, is_current=lambda: True,
            grounded_responses=True, current_player_event_id='actual')
        assert len(requests) == 1
        assert result.jobs[0].objective == '' and result.jobs[0].delivery_constraints == []
        assert result.jobs[0].grounded_response.purpose == 'invite_player'
        assert result.jobs[0].grounded_response.event_id == 'actual'
        assert 'kind' not in result.jobs[0].model_dump()
    finally:
        await provider.close()
