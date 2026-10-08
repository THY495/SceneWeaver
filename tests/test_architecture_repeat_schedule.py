"""Repeated experiments must balance order within inputs and preserve fixtures."""
from pathlib import Path


def test_repeat_schedule_balances_each_input(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'scripts'))
    from probe_response_architecture import CASES, trial_schedule

    schedule = trial_schedule(2)
    assert len(schedule) == 8
    for case, message in CASES:
        positions = [(i, repeat) for i, (repeat, key, text) in enumerate(schedule)
                     if (key, text) == (case, message)]
        assert {repeat for _, repeat in positions} == {1, 2}
        assert {i % 2 for i, _ in positions} == {0, 1}
    assert trial_schedule(1) == [(1, *row) for row in CASES]


def test_repeat_fixture_preserves_authorized_reply(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'scripts'))
    from probe_response_architecture import fixture

    beat = fixture()['narrative']['nodes'][0]['direction']['beats'][0]
    assert beat['direct_response_actor'] == 'captain'
    assert beat['participants'] == ['captain'] and beat['max_segments'] == 1
    assert not beat['constraints'] and not beat['role_focus']


def test_two_case_authorization_repeats_reverse_order(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'scripts'))
    from probe_response_architecture import trial_schedule

    schedule = trial_schedule(2, 'two')
    assert len(schedule) == 4
    for case in {'unknown_schedule', 'identity'}:
        positions = [(i, repeat) for i, (repeat, name, _) in enumerate(schedule) if name == case]
        assert {repeat for _, repeat in positions} == {1, 2}
        assert {i % 2 for i, _ in positions} == {0, 1}
