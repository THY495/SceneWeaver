"""Evidence checks shared by the finite independent-game acceptance clients."""

import time


def wait_for_final_trace(read_ready, *, timeout=2, clock=time.monotonic, sleep=time.sleep):
    """A terminal task may precede the worker's final timing flush. No new work."""
    deadline = clock() + timeout
    while True:
        if read_ready():
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(.05, remaining))


def game_run_url(base, save_id):
    """Keep the run prefix unambiguous to the player's pathname router."""
    return base.rstrip('/') + '/runs/' + save_id + '/'


def handoff_inputs(identity):
    """Bind fixed player inputs to an explicit authored test variant."""
    variant = identity.get('derived', {}).get('handoff_variant')
    if variant == 'single_question_chain_v1':
        return ['阿岚，先只问我是否先聊公告；等我回答后，沈舟接你的话，林澈再接沈舟的话。',
            '是的，先聊公告。']
    if variant is not None:
        raise ValueError('Unknown authored handoff variant')
    return ['阿岚，先问问我想谈什么，再让你们三人交流。', '是的，先谈公告。']


def handoff_question_ready(identity, first):
    """Check the first report before spending on the second fixed input."""
    variant = identity.get('derived', {}).get('handoff_variant')
    questions = first.get('player_questions', [])
    if variant is None:
        return bool(questions)
    return variant == 'single_question_chain_v1' and len(questions) == 1


def formal_audio(speech, states, event_ids):
    """Require each chunk, not an arbitrary number of successful replays."""
    completed = {event_id: set() for event_id in event_ids}
    failures = []
    for state in states:
        if state.get('event_id') not in completed:
            continue
        if state.get('status') in {'failed', 'interrupted', 'cancelled'}:
            failures.append(state)
        chunk = state.get('chunk_index')
        if (type(chunk) is int and chunk >= 0 and state.get('status') == 'completed'
                and state.get('stream_done') is True and state.get('played_samples', 0) > 0):
            completed[state['event_id']].add(chunk)
    ids = [row['id'] for row in speech]
    covered = bool(event_ids) and len(ids) == len(set(ids)) and set(ids) == set(event_ids)
    all_played = covered and not failures and all(
        bool(row['chunks']) and not row['text_fallback']
        and set(range(len(row['chunks']))) <= completed[row['id']] for row in speech)
    return {'speech': speech, 'states': states, 'failed_states': failures,
        'completed_chunks_by_event': {key: sorted(value) for key, value in completed.items()},
        'all_played': all_played}


def handoff_observation(steps):
    """Describe separate evidence dimensions, without changing acceptance gates.

    Committed source proof is structural. Neither a link nor a reuse hit
    establishes relevance, distinct character contributions or author intent.
    """
    segments = [row for step in steps for row in step.get('segments', [])]
    links = {}
    for step in steps:
        for row in step.get('source_authorizations', []):
            fields = ('source_event_id', 'response_event_id', 'source_actor_id', 'response_actor_id')
            if (row.get('verified_committed_link') is not True
                    or not all(isinstance(row.get(key), str) and row[key] for key in fields)
                    or row['source_actor_id'] == row['response_actor_id']):
                continue
            identity = tuple(row[key] for key in fields)
            links[identity] = {key: row[key] for key in fields}
    chain = any(
        first['response_event_id'] == second['source_event_id']
        and first['response_actor_id'] == second['source_actor_id']
        and len({first['source_actor_id'], first['response_actor_id'], second['response_actor_id']}) == 3
        for first in links.values() for second in links.values()
    )
    voice_complete = bool(steps) and all(
        formal_audio(step.get('audio', {}).get('speech', []), step.get('audio', {}).get('states', []),
            {row['event_id'] for row in step.get('segments', [])})['all_played']
        for step in steps
    )
    return {
        'scope': 'Observed report fields; structural evidence, not a semantic or overall pass verdict',
        'all_turns_completed': bool(steps) and all(step.get('status') == 'completed'
            and step.get('stop_reason') != 'safety_rejected' for step in steps),
        'formal_event_count': len({row['event_id'] for row in segments}),
        'distinct_speaker_count': len({row['actor_id'] for row in segments if row.get('actor_id')}),
        'formal_audio_complete_for_every_turn': voice_complete,
        'npc_reply_links': list(links.values()),
        'three_speaker_source_chain_observed': chain,
        'reuse_observed': any(bool(step.get('reuse_link')) for step in steps),
        'semantic_relevance': 'not_assessed',
        'author_contribution': 'not_assessed',
    }


def authored_handoff_complete(identity, inputs, steps):
    """Additional gates for the named variant; old report semantics stay intact."""
    variant = identity.get('derived', {}).get('handoff_variant')
    if variant is None:
        return True
    if variant != 'single_question_chain_v1' or inputs != handoff_inputs(identity):
        return False
    if (len(steps) != 2 or not handoff_question_ready(identity, steps[0])
            or steps[1].get('player_questions')):
        return False
    observed = handoff_observation(steps)
    if observed['distinct_speaker_count'] != 3:
        return False
    committed = {row['event_id']: row.get('actor_id')
        for step in steps for row in step.get('segments', [])}
    order = {row['event_id']: index for index, row in enumerate(
        row for step in steps for row in step.get('segments', []))}
    links = [row for row in observed['npc_reply_links']
        if committed.get(row['source_event_id']) == row['source_actor_id']
        and committed.get(row['response_event_id']) == row['response_actor_id']
        and order[row['source_event_id']] < order[row['response_event_id']]]
    return any(
        first['response_event_id'] == second['source_event_id']
        and first['response_actor_id'] == second['source_actor_id']
        and len({first['source_actor_id'], first['response_actor_id'], second['response_actor_id']}) == 3
        for first in links for second in links
    )


def completed_handoff(prior, identity):
    """The new contract cannot borrow the legacy directed-voice success flag."""
    if (prior.get('identity') != identity or not prior.get('save_id')
            or prior.get('completed_fixed_handoff') is not True or prior.get('page_errors')):
        return False
    steps = prior.get('steps', [])
    if (len(steps) != 2 or not steps[0].get('player_questions')
            or not steps[1].get('reuse_link')):
        return False
    for step in steps:
        segments, audio = step.get('segments', []), step.get('audio', {})
        if (step.get('status') != 'completed' or step.get('stop_reason') == 'safety_rejected'
                or not segments or not formal_audio(audio.get('speech', []), audio.get('states', []),
                    {row['event_id'] for row in segments})['all_played']):
            return False
    if not authored_handoff_complete(identity, prior.get('fixed_inputs'), steps):
        return False
    # Structural source linkage is distinct from a human judgment of relevance.
    return any(row.get('verified_committed_link') is True
        for step in steps for row in step.get('source_authorizations', []))


def compatible_source_report(prior, identity):
    if prior is None:
        return True
    if prior.get('identity') != identity or not prior.get('save_id'):
        return False
    if 'completed_fixed_handoff' in prior:
        return completed_handoff(prior, identity)
    return prior.get('directed_voice_contract_met') is True
