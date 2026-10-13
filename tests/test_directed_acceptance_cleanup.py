"""Exercise the real acceptance entry without model or database connections."""

import json
import sys
from types import SimpleNamespace

import pytest

from scripts import accept_directed_ferry_live as probe


@pytest.fixture
def entry(tmp_path, monkeypatch, config):
    output = tmp_path / '.runtime/result.json'
    calls = []
    config.api_base = 'https://api.deepseek.com'
    config.daily_budget_usd = 6
    monkeypatch.setattr(probe, 'ROOT', tmp_path)
    monkeypatch.setattr(probe, 'Settings', lambda: config)
    monkeypatch.setenv('TEST_DATABASE_URL', 'postgresql+psycopg://test:test@127.0.0.1:55432/cleanup_test')
    monkeypatch.setattr(sys, 'argv', ['accept_directed_ferry_live.py', '--output', str(output)])

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, statement):
            sql = str(statement)
            calls.append(sql)
            if sql.startswith('DROP') and state.drop_error:
                raise RuntimeError('owned database still has a connection')

    class Admin:
        def connect(self):
            return Connection()

        def dispose(self):
            calls.append('admin-disposed')

    class Database:
        def __init__(self, settings):
            self.label = 'budget' if settings is config else 'isolated'

        def initialize(self):
            calls.append(self.label + '-initialize')

        def close(self):
            calls.append(self.label + '-closed')

    state = SimpleNamespace(output=output, calls=calls, drop_error=False, settled=False)
    monkeypatch.setattr(probe, 'create_engine', lambda *a, **k: Admin())
    monkeypatch.setattr(probe, 'Database', Database)
    monkeypatch.setattr(probe, 'reserve_acceptance_budget', lambda *a: 'controlled-reservation')
    monkeypatch.setattr(probe, 'ledger', lambda *a: {'cost_usd': 0.031, 'reserved_usd': 0.007})

    def settle(*args):
        assert args[1] == 'controlled-reservation'
        assert args[2] == pytest.approx(0.038)
        state.settled = True
        calls.append('settled')

    async def run(*args):
        calls.append('controlled-run-finished')
        return {'passed': True, 'task': 'controlled', 'cost_usd': 0.031}

    monkeypatch.setattr(probe, 'settle_acceptance_budget', settle)
    monkeypatch.setattr(probe, 'run', run)
    return state


@pytest.mark.parametrize('failure', ['ledger', 'ledger_receipt', 'settlement', 'budget_receipt'])
def test_accounting_failure_preserves_owned_evidence_database(entry, monkeypatch, failure):
    def fail(*args):
        raise RuntimeError('controlled accounting failure')

    if failure == 'ledger':
        monkeypatch.setattr(probe, 'ledger', fail)
    elif failure == 'settlement':
        monkeypatch.setattr(probe, 'settle_acceptance_budget', fail)
    else:
        original = type(entry.output).write_text
        suffix = '-ledger.json' if failure == 'ledger_receipt' else '-project-budget.json'

        def write(path, *args, **kwargs):
            if path.name.endswith(suffix):
                fail()
            return original(path, *args, **kwargs)

        monkeypatch.setattr(type(entry.output), 'write_text', write)
    with pytest.raises(RuntimeError, match='controlled accounting failure'):
        probe.main()
    assert not any(call.startswith('DROP') for call in entry.calls)
    pending = json.loads(entry.output.with_name('result-cleanup-pending.json').read_text('utf-8'))
    assert pending['database_name'].startswith('sceneweaver_directed_')
    assert pending['database_name'].endswith('_test')
    assert pending['project_budget_reservation'] == 'controlled-reservation'
    assert pending['accounting_phase'] == failure
    assert pending['project_budget_status'] == (
        'completed' if failure == 'budget_receipt' else 'unknown' if failure == 'settlement' else 'not_attempted'
    )
    assert entry.calls[-1] == 'admin-disposed'
    assert 'isolated-closed' in entry.calls and 'budget-closed' in entry.calls
    assert entry.settled is (failure == 'budget_receipt')


def test_success_closes_connections_then_uses_nonforced_drop(entry):
    probe.main()
    drops = [call for call in entry.calls if call.startswith('DROP')]
    assert len(drops) == 1 and 'FORCE' not in drops[0]
    assert entry.calls.index('settled') < entry.calls.index('isolated-closed') < entry.calls.index(drops[0])
    assert entry.calls.index('budget-closed') < entry.calls.index(drops[0])
    assert not entry.output.with_name('result-cleanup-pending.json').exists()


def test_busy_database_cleanup_preserves_pending_receipt_without_forcing(entry):
    entry.drop_error = True
    with pytest.raises(RuntimeError, match='still has a connection'):
        probe.main()
    drops = [call for call in entry.calls if call.startswith('DROP')]
    assert len(drops) == 1 and 'FORCE' not in drops[0]
    pending = json.loads(entry.output.with_name('result-cleanup-pending.json').read_text('utf-8'))
    assert 'cleanup failed' in pending['reason']
    assert pending['project_budget_reservation'] == 'controlled-reservation'
    assert pending['project_budget_status'] == 'completed'
    assert entry.settled and entry.calls[-1] == 'admin-disposed'


def test_secondary_pending_receipt_failure_keeps_original_error(entry, monkeypatch):
    def fail_settlement(*args):
        raise RuntimeError('original settlement failure')

    original = type(entry.output).write_text

    def write(path, *args, **kwargs):
        if path.name.endswith('-cleanup-pending.json'):
            raise OSError('secondary receipt failure')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(probe, 'settle_acceptance_budget', fail_settlement)
    monkeypatch.setattr(type(entry.output), 'write_text', write)
    with pytest.raises(RuntimeError, match='original settlement failure') as error:
        probe.main()
    assert any('could not be persisted' in note for note in error.value.__notes__)
    assert not any(call.startswith('DROP') for call in entry.calls)
    assert entry.calls[-1] == 'admin-disposed'
