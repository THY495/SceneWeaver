"""Do not turn duplicated audio or an incomplete report into acceptance."""

import copy
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def contract(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'scripts'))
    return importlib.import_module('ferry_acceptance_contract')


@pytest.mark.parametrize('base', ['http://127.0.0.1:1234', 'http://127.0.0.1:1234/'])
def test_player_run_url_has_one_path_separator(contract, base):
    assert contract.game_run_url(base, 'same-save') == 'http://127.0.0.1:1234/runs/same-save/'


@pytest.mark.parametrize('arrives', [True, False])
def test_final_trace_wait_is_bounded_and_only_observes(contract, arrives):
    tick, reads = [0.0], []

    def read():
        reads.append(tick[0])
        return arrives and len(reads) >= 3

    def sleep(seconds):
        tick[0] += seconds

    assert contract.wait_for_final_trace(read, timeout=.2, clock=lambda: tick[0], sleep=sleep) is arrives
    assert tick[0] <= .2
    assert len(reads) >= 3


def speech(event='line'):
    return {'id': event, 'chunks': [{}, {}], 'text_fallback': False}


def played(chunk, event='line'):
    return {'event_id': event, 'chunk_index': chunk, 'status': 'completed',
        'stream_done': True, 'played_samples': 100}


@pytest.mark.parametrize('states', [[played(0), played(0)], [played(0), played(2)],
    [played(0), {**played(1), 'status': 'failed'}],
    [played(0), {**played(1), 'played_samples': 0}],
    [played(0), {**played(1), 'stream_done': False}],
    [played(0), {**played(1), 'chunk_index': True}], [played(0)]])
def test_missing_or_failed_formal_chunk_is_not_playback_success(contract, states):
    assert not contract.formal_audio([speech()], states, {'line'})['all_played']


def test_unique_chunks_and_same_actor_later_event(contract):
    states = [played(0), played(0), played(1), played(0, 'later')]
    audio = contract.formal_audio([speech()], states, {'line'})
    assert audio['all_played']
    assert audio['completed_chunks_by_event'] == {'line': [0, 1]}
    assert not contract.formal_audio([speech(), speech('later')], states, {'line', 'later'})['all_played']


def valid_report():
    identity = {'project': 'fixed', 'source_web': True}
    step = {'status': 'completed', 'segments': [{'event_id': 'line'}],
        'audio': {'speech': [speech()], 'states': [played(0), played(1)]},
        'source_authorizations': [{'verified_committed_link': True}]}
    first, second = copy.deepcopy(step), copy.deepcopy(step)
    first['player_questions'] = [{'id': 'actual-question'}]
    second['reuse_link'] = {'source_task_id': 'first'}
    return {'identity': identity, 'save_id': 'same-save', 'completed_fixed_handoff': True,
        'steps': [first, second], 'page_errors': []}, identity


def test_new_report_contract_does_not_require_forging_old_flag(contract):
    report, identity = valid_report()
    assert 'directed_voice_contract_met' not in report
    assert contract.compatible_source_report(report, identity)
    assert not contract.compatible_source_report(report, {'project': 'other'})


@pytest.mark.parametrize('fault', ['incomplete', 'missing_chunk', 'missing_dependency', 'page_error', 'no_save'])
def test_legacy_success_flag_cannot_bypass_incomplete_new_report(contract, fault):
    report, identity = valid_report()
    report['directed_voice_contract_met'] = True
    if fault == 'incomplete':
        report['completed_fixed_handoff'] = False
    elif fault == 'missing_chunk':
        report['steps'][1]['audio']['states'] = [played(0), played(0)]
    elif fault == 'missing_dependency':
        for step in report['steps']:
            step['source_authorizations'][0]['verified_committed_link'] = False
    elif fault == 'page_error':
        report['page_errors'] = ['runtime error']
    else:
        report['save_id'] = None
    assert not contract.compatible_source_report(report, identity)


def source_link(source='shen', target='alan', event='shen-line', response='alan-line'):
    return dict(verified_committed_link=True, source_actor_id=source, response_actor_id=target,
        source_event_id=event, response_event_id=response)


def test_observed_new_planning_and_audio_are_not_erased_by_missing_reuse(contract):
    report, identity = valid_report()
    report['completed_fixed_handoff'] = False
    del report['steps'][1]['reuse_link']
    report['steps'][1]['source_authorizations'] = [source_link()]
    before = copy.deepcopy(report)
    observed = contract.handoff_observation(report['steps'])
    assert observed['all_turns_completed'] and observed['formal_audio_complete_for_every_turn']
    assert len(observed['npc_reply_links']) == 1 and not observed['reuse_observed']
    assert observed['semantic_relevance'] == observed['author_contribution'] == 'not_assessed'
    assert not contract.completed_handoff(report, identity)
    assert report == before


@pytest.mark.parametrize('serial', [True, False])
def test_two_replies_to_one_speaker_are_not_a_three_speaker_chain(contract, serial):
    report, _ = valid_report()
    first = source_link()
    second = source_link('alan' if serial else 'shen', 'lin',
        'alan-line' if serial else 'shen-line', 'lin-line')
    report['steps'][1]['source_authorizations'] = [first, second, copy.deepcopy(first)]
    observed = contract.handoff_observation(report['steps'])
    assert len(observed['npc_reply_links']) == 2
    assert observed['three_speaker_source_chain_observed'] is serial


@pytest.mark.parametrize('fault', ['unverified', 'same_actor', 'missing_id', 'fake_flag'])
def test_unproven_links_do_not_count_as_exchange(contract, fault):
    link = source_link()
    if fault == 'unverified':
        link['verified_committed_link'] = False
    elif fault == 'same_actor':
        link['response_actor_id'] = link['source_actor_id']
    elif fault == 'missing_id':
        del link['source_event_id']
    else:
        link['verified_committed_link'] = 'true'
    report, _ = valid_report()
    report['steps'][0]['source_authorizations'] = []
    report['steps'][1]['source_authorizations'] = [link]
    assert not contract.handoff_observation(report['steps'])['npc_reply_links']


def test_completion_cannot_borrow_voice_flag_or_later_failed_turn(contract):
    report, _ = valid_report()
    report['steps'][1]['audio']['all_played'] = True
    report['steps'][1]['audio']['states'] = [played(0), played(0)]
    observed = contract.handoff_observation(report['steps'])
    assert observed['all_turns_completed'] and not observed['formal_audio_complete_for_every_turn']
    report['steps'][1].update(status='failed', segments=[])
    observed = contract.handoff_observation(report['steps'])
    assert not observed['all_turns_completed'] and not observed['formal_audio_complete_for_every_turn']


def chain_report(contract):
    report, identity = valid_report()
    identity['derived'] = {'handoff_variant': 'single_question_chain_v1'}
    report['fixed_inputs'] = contract.handoff_inputs(identity)
    first, second = report['steps']
    first['segments'] = [{'event_id': 'captain-question', 'actor_id': 'captain'}]
    first['audio'] = {'speech': [speech('captain-question')],
        'states': [played(0, 'captain-question'), played(1, 'captain-question')]}
    first['source_authorizations'] = []
    second['segments'] = [{'event_id': 'keeper-answer', 'actor_id': 'keeper'},
        {'event_id': 'lin-answer', 'actor_id': 'lin'}]
    second['audio'] = {'speech': [speech('keeper-answer'), speech('lin-answer')],
        'states': [played(chunk, event) for event in ('keeper-answer', 'lin-answer') for chunk in (0, 1)]}
    second['source_authorizations'] = [
        source_link('captain', 'keeper', 'captain-question', 'keeper-answer'),
        source_link('keeper', 'lin', 'keeper-answer', 'lin-answer'),
    ]
    return report, identity


def test_inputs_are_bound_to_explicit_author_variant(contract):
    old = contract.handoff_inputs({})
    report, identity = chain_report(contract)
    assert old != report['fixed_inputs'] and '先只问我是否先聊公告' in report['fixed_inputs'][0]
    assert contract.completed_handoff(report, identity)
    with pytest.raises(ValueError, match='Unknown authored'):
        contract.handoff_inputs({'derived': {'handoff_variant': 'unreviewed'}})


@pytest.mark.parametrize('count', [0, 1, 2])
def test_first_question_count_is_checked_before_second_input(contract, count):
    first = {'player_questions': [{'id': str(index)} for index in range(count)]}
    identity = {'derived': {'handoff_variant': 'single_question_chain_v1'}}
    assert contract.handoff_question_ready(identity, first) is (count == 1)
    assert contract.handoff_question_ready({}, first) is (count > 0)


@pytest.mark.parametrize('fault', ['old_inputs', 'fanout', 'no_reuse', 'missing_speaker', 'partial_audio',
    'unknown_variant', 'invented_source', 'wrong_response_actor', 'multiple_questions',
    'new_question', 'wrong_order'])
def test_new_author_variant_requires_its_inputs_reuse_and_continuous_chain(contract, fault):
    report, identity = chain_report(contract)
    if fault == 'old_inputs':
        report['fixed_inputs'] = contract.handoff_inputs({})
    elif fault == 'fanout':
        report['steps'][1]['source_authorizations'][1] = source_link(
            'captain', 'lin', 'captain-question', 'lin-answer')
    elif fault == 'no_reuse':
        del report['steps'][1]['reuse_link']
    elif fault == 'missing_speaker':
        report['steps'][1]['segments'][1]['actor_id'] = 'keeper'
    elif fault == 'partial_audio':
        report['steps'][1]['audio']['states'].pop()
    elif fault == 'unknown_variant':
        identity['derived']['handoff_variant'] = 'unreviewed'
    elif fault == 'invented_source':
        report['steps'][1]['source_authorizations'][0]['source_event_id'] = 'not-in-this-run'
    elif fault == 'wrong_response_actor':
        report['steps'][1]['source_authorizations'][1]['response_actor_id'] = 'unpublished-npc'
    elif fault == 'multiple_questions':
        report['steps'][0]['player_questions'].append({'id': 'other-question'})
    elif fault == 'new_question':
        report['steps'][1]['player_questions'] = [{'id': 'new-player-question'}]
    else:
        report['steps'][1]['segments'].reverse()
    report['observation'] = {'three_speaker_source_chain_observed': True}
    assert not contract.completed_handoff(report, identity)
