import json
import pytest
from pathlib import Path
from helio_security.live_ledger import (
    LiveLedger,
    BudgetExceededError,
    calculate_cost,
)


def test_calculate_cost_accuracy():
    # Long-context pricing: $0.50/M input and $2.50/M output.
    cost = calculate_cost(1_000_000, 1_000_000, model="claude-haiku-5-5")
    assert round(cost, 4) == 3.00

    # 1,000 in, 500 out
    cost_small = calculate_cost(1000, 500, model="claude-haiku-5-5")
    # 1000 * 0.10 / 1e6 = 0.0001
    # 500 * 0.50 / 1e6 = 0.00025
    assert round(cost_small, 6) == 0.00035


def test_ledger_initialization_and_defaults(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    ledger = LiveLedger(ledger_path=ledger_path, max_spend_usd=1.00, max_requests=10)
    data = ledger.get_data()

    assert data["total_requests"] == 0
    assert data["total_estimated_cost_usd"] == 0.0
    assert data["max_spend_usd"] == 1.00
    assert data["max_requests"] == 10
    assert data["runs"] == []
    assert ledger_path.exists()


def test_record_request_tracks_tokens_and_cost(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    ledger = LiveLedger(ledger_path=ledger_path, max_spend_usd=1.00, max_requests=10)

    ledger.record_request(model="claude-haiku-5-5", tokens_in=2000, tokens_out=300, status="success")
    data = ledger.get_data()

    assert data["total_requests"] == 1
    assert data["total_input_tokens"] == 2000
    assert data["total_output_tokens"] == 300
    expected_cost = calculate_cost(2000, 300, model="claude-haiku-5-5")
    assert round(data["total_estimated_cost_usd"], 6) == round(expected_cost, 6)
    assert len(data["runs"]) == 1
    assert data["runs"][0]["status"] == "success"


def test_can_execute_enforces_max_requests_limit(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    ledger = LiveLedger(ledger_path=ledger_path, max_spend_usd=1.00, max_requests=2)

    assert ledger.can_execute(est_in=1000, est_out=500) is True
    ledger.record_request(model="claude-haiku-5-5", tokens_in=1000, tokens_out=500)

    assert ledger.can_execute(est_in=1000, est_out=500) is True
    ledger.record_request(model="claude-haiku-5-5", tokens_in=1000, tokens_out=500)

    # Now total_requests == 2 == max_requests
    assert ledger.can_execute(est_in=1000, est_out=500) is False
    with pytest.raises(BudgetExceededError, match="Request count ceiling"):
        ledger.check_and_reserve(est_in=1000, est_out=500)


def test_can_execute_enforces_spending_limit(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    # Set tight spending limit $0.001
    ledger = LiveLedger(ledger_path=ledger_path, max_spend_usd=0.0005, max_requests=10)

    # 1000 in, 500 out = $0.00035 <= $0.0005
    assert ledger.can_execute(est_in=1000, est_out=500) is True
    ledger.record_request(model="claude-haiku-5-5", tokens_in=1000, tokens_out=500)

    # Next request would cost $0.00035, total would be $0.00070 > $0.0005
    assert ledger.can_execute(est_in=1000, est_out=500) is False
    with pytest.raises(BudgetExceededError, match="Spending limit"):
        ledger.check_and_reserve(est_in=1000, est_out=500)


def test_reservation_survives_restart_before_result(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = LiveLedger(path, max_requests=1)
    ledger.check_and_reserve()
    restarted = LiveLedger(path, max_requests=1)
    with pytest.raises(BudgetExceededError):
        restarted.check_and_reserve()


def test_corrupt_ledger_does_not_reset_allowance(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = LiveLedger(path, max_requests=1)
    ledger.record_request("claude-haiku-5-5", 100, 100)
    path.write_text("{corrupt")
    with pytest.raises((BudgetExceededError, ValueError)):
        ledger.check_and_reserve()


def test_unknown_model_pricing_fails_closed(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = LiveLedger(path, max_requests=10)
    with pytest.raises(BudgetExceededError, match="Unknown pricing"):
        ledger.check_and_reserve(model="gpt-5-omni-expensive")

