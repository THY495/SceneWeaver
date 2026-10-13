"""Isolated acceptances cannot bypass or rewrite the shared project envelope."""

import pytest

from chatbot.storage import Usage
from scripts.acceptance_scope import reserve_acceptance_budget, settle_acceptance_budget


def test_shared_cap_includes_other_reserved_batches(db, config):
    config.daily_budget_usd = 0.3
    first = reserve_acceptance_budget(db, config, 0.2)
    with pytest.raises(RuntimeError, match="Insufficient daily budget"):
        reserve_acceptance_budget(db, config, 0.2)
    settle_acceptance_budget(db, first, 0.04)
    second = reserve_acceptance_budget(db, config, 0.2)
    assert first != second


def test_failed_settlement_keeps_reservation_and_does_not_erase_evidence(db, config):
    reservation = reserve_acceptance_budget(db, config, 0.2)
    with pytest.raises(ValueError, match="nonnegative and finite"):
        settle_acceptance_budget(db, reservation, float("nan"))
    with db.session() as s:
        row = s.get(Usage, reservation)
        assert row.status == "reserved" and row.reserved == 0.2 and row.cost == 0


def test_charge_is_idempotent_but_cannot_be_repriced(db, config):
    reservation = reserve_acceptance_budget(db, config, 0.2)
    settle_acceptance_budget(db, reservation, 0.031)
    settle_acceptance_budget(db, reservation, 0.031)
    with pytest.raises(ValueError, match="cannot be overwritten"):
        settle_acceptance_budget(db, reservation, 0)
    with db.session() as s:
        row = s.get(Usage, reservation)
        assert row.cost == 0.031 and row.reserved == 0 and row.status == "completed"


def test_missing_or_foreign_usage_cannot_be_settled(db, config):
    with pytest.raises(ValueError, match="Missing acceptance"):
        settle_acceptance_budget(db, "missing", 0)
    with db.session.begin() as s:
        s.add(Usage(id="other-usage", owner_id="test", task_id="other", role="dialogue", model="fake", reserved=0.1))
    with pytest.raises(ValueError, match="Missing acceptance"):
        settle_acceptance_budget(db, "other-usage", 0)
    with db.session() as s:
        assert s.get(Usage, "other-usage").reserved == 0.1
