"""Recover a redundant root close without supplying a missing review verdict."""

import json
from copy import deepcopy
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from chatbot.observability.timing import TimingTrace
from chatbot.providers import OutputTruncated, Provider, StructuredOutputError, chat_request_messages
from chatbot.review_json import repair_actor_review_root_close
from chatbot.runtime import StaleTask
from chatbot.schemas import ActorReview
from chatbot.storage import Operation, Task, Usage, now


def review(accepted=False):
    return dict(checks=[dict(text='我昨日见过他。', basis='没有已确认的相见记录。',
        supported=False, reason_code='invented_history')], accepted=accepted,
        delivery=dict(addressee_id='player', expects_reply=False, response_kind='answer'),
        issues=['过去经历无依据。'])


def premature_close(value):
    checks = json.dumps({'checks': value['checks']}, ensure_ascii=False)
    rest = json.dumps({k: v for k, v in value.items() if k != 'checks'}, ensure_ascii=False)
    return checks + ', ' + rest[1:]


@pytest.mark.parametrize('accepted', [False, True])
def test_single_root_close_repair_preserves_explicit_fields_and_rejection(accepted):
    original = review(accepted)
    invalid = premature_close(original)
    corrected = repair_actor_review_root_close(invalid)
    assert corrected is not None
    assert len(invalid) == len(corrected) + 1
    assert json.loads(corrected) == original
    actual = ActorReview.model_validate_json(corrected).model_dump()
    assert actual['accepted'] is accepted
    assert actual['checks'][0]['supported'] is False
    assert actual['issues'] == original['issues']
    assert not (actual['accepted'] and not actual['issues'] and all(c['supported'] for c in actual['checks']))


def test_quotation_braces_and_whitespace_are_not_changed():
    original = review()
    original['checks'][0]['basis'] = '原话包含 } 和 ], "accepted": true；这里只是字符串。'
    invalid = ' \n ' + premature_close(original) + ' \n '
    corrected = repair_actor_review_root_close(invalid)
    assert json.loads(corrected) == original
    assert corrected.startswith(' \n ') and corrected.endswith(' \n ')
    assert original['checks'][0]['basis'] in json.loads(corrected)['checks'][0]['basis']


@pytest.mark.parametrize('change', [
    lambda r: r.pop('accepted'),
    lambda r: r.pop('delivery'),
    lambda r: r.pop('issues'),
    lambda r: r.update(accepted=1),
    lambda r: r.update(accepted='false'),
    lambda r: r.update(extra='unrecognized'),
    lambda r: r['delivery'].update(expects_reply='false'),
    lambda r: r['delivery'].update(unrecognized='field'),
    lambda r: r['delivery'].pop('response_kind'),
    lambda r: r['checks'][0].update(supported='false'),
    lambda r: r['checks'][0].pop('basis'),
    lambda r: r['checks'][0].update(reason_code='unknown'),
    lambda r: r['checks'][0].update(unrecognized='field'),
])
def test_missing_ambiguous_or_invalid_fields_are_not_repaired(change):
    original = deepcopy(review())
    change(original)
    assert repair_actor_review_root_close(premature_close(original)) is None


@pytest.mark.parametrize('suffix', ['', ' trailing explanation', '\n{}', '}'])
def test_valid_or_unfinished_or_extra_documents_are_not_repaired(suffix):
    assert repair_actor_review_root_close(json.dumps(review()) + suffix) is None


@pytest.mark.parametrize('mutate', [
    lambda s: s.replace('"accepted": false', '"accepted": false, "accepted": true'),
    lambda s: s.replace('"supported": false', '"supported": false, "supported": true'),
    lambda s: s.replace('"accepted": false', '"accept\\u0065d": false, "accepted": true'),
    lambda s: s.replace('"accepted": false', '"accepted": NaN'),
    lambda s: s.replace('}]', '}}]'),
    lambda s: '```json\n' + s + '\n```',
    lambda s: s + ' explanation',
    lambda s: s + '\n{"accepted":true}',
    lambda s: s.replace(']},', ']}},'),
])
def test_duplicate_or_non_json_or_multiple_edit_shapes_decline(mutate):
    invalid = premature_close(review())
    assert repair_actor_review_root_close(mutate(invalid)) is None


def test_empty_wrong_root_and_oversized_inputs_decline():
    for invalid in [None, {}, '', '[]', 'null', 'true', ' ' * 65537, '{"accepted":', premature_close(review())[:-1]]:
        assert repair_actor_review_root_close(invalid) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('accepted,supported,issues', [
    (False, True, []), (True, False, []), (True, True, ['待确认。']), (True, True, []),
])
async def test_local_repair_uses_one_request_and_preserves_review_usage_cache_and_privacy(
    db, config, runtime, accepted, supported, issues,
):
    conversation = runtime.create_session()
    runtime.submit(conversation.id, 'local', '你好', 'local-json-repair')
    task = runtime.claim('json-repair-worker')
    original = review(accepted)
    original['checks'][0].update(text='private-reviewed-line', supported=supported)
    original['issues'] = issues
    invalid = premature_close(original)
    requests = []
    messages = [dict(role='user', content='original-candidate-and-evidence')]
    trace = TimingTrace(task)

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=dict(choices=[dict(finish_reason='stop', message=dict(content=invalid))],
            usage=dict(prompt_tokens=0, completion_tokens=0)))

    provider = Provider(db, config, runtime)
    await provider.http.aclose()
    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        with trace.activate():
            actual = await provider.chat(task.id, 'actor_review', messages, schema=ActorReview,
                                         operation='local-review')
            cached = await provider.chat(task.id, 'actor_review', messages, schema=ActorReview,
                                         operation='local-review')
        assert actual == cached == ActorReview.model_validate(original, strict=True).model_dump()
        assert actual['accepted'] is accepted and actual['checks'][0]['supported'] is supported
        assert actual['issues'] == issues
        assert bool(actual['accepted'] and not actual['issues'] and all(c['supported'] for c in actual['checks'])) == (
            accepted and supported and not issues
        )
        assert len(requests) == 1
        assert requests[0]['messages'] == chat_request_messages(messages, ActorReview)
        local = [s for s in trace.spans if s['name'] == 'model.local_json_repair']
        assert len(local) == 1 and local[0]['attributes']['review_json_repair_kind'] == 'redundant_root_close_after_checks'
        assert not any(s['name'] in {'model.retry_wait', 'model.format_repair'} for s in trace.spans)
        trace.save(db)
        with db.session() as store:
            usage = list(store.scalars(select(Usage).where(Usage.task_id == task.id)))
            assert len(usage) == 1 and usage[0].status == 'completed' and usage[0].reserved == usage[0].cost == 0
            operation = store.get(Operation, f'{task.id}:local-review')
            assert operation.payload['value'] == actual
            persisted = [o.payload for o in store.scalars(select(Operation).where(Operation.task_id == task.id))
                         if 'timing_v1' in o.payload]
            assert 'private-reviewed-line' not in json.dumps(persisted)
            assert 'original-candidate-and-evidence' not in json.dumps(persisted)
    finally:
        await provider.close()


class ExtendedReview(ActorReview):
    extra_verdict: bool


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['different-role', 'extended-schema', 'truncated', 'stale-lease'])
async def test_ineligible_outputs_and_stale_lease_never_repair_or_cache(db, config, runtime, mode):
    conversation = runtime.create_session()
    runtime.submit(conversation.id, 'local', '你好', 'ineligible-local-repair')
    task = runtime.claim('json-repair-worker')
    original = review()
    if mode == 'extended-schema':
        original['extra_verdict'] = True
    requests = []
    trace = TimingTrace(task)

    def respond(request):
        requests.append(json.loads(request.content))
        if mode == 'stale-lease':
            with db.session.begin() as store:
                store.get(Task, task.id).lease_until = now() - timedelta(seconds=1)
        return httpx.Response(200, json=dict(choices=[dict(
            finish_reason='length' if mode == 'truncated' else 'stop',
            message=dict(content=premature_close(original)))], usage=dict(prompt_tokens=0, completion_tokens=0)))

    provider = Provider(db, config, runtime)
    await provider.http.aclose()
    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    schema = ExtendedReview if mode == 'extended-schema' else ActorReview
    role = 'dialogue' if mode == 'different-role' else 'actor_review'
    expected_error = {'truncated': OutputTruncated, 'stale-lease': StaleTask}.get(mode, StructuredOutputError)
    try:
        with trace.activate():
            with pytest.raises(expected_error):
                await provider.chat(task.id, role, [], schema=schema, max_attempts=1, operation='ineligible-review')
        assert len(requests) == 1
        assert not any(s['name'] == 'model.local_json_repair' for s in trace.spans)
        with db.session() as store:
            usage = list(store.scalars(select(Usage).where(Usage.task_id == task.id)))
            assert len(usage) == 1 and usage[0].status == 'failed' and usage[0].reserved == usage[0].cost == 0
            assert store.get(Operation, f'{task.id}:ineligible-review') is None
    finally:
        await provider.close()
