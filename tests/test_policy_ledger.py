import pytest

from aibtc_nexus.errors import DuplicatePayment, PolicyViolation
from aibtc_nexus.ledger import Ledger
from aibtc_nexus.policy import PolicyEngine, SpendingPolicy


def engine(**kw):
    return PolicyEngine(SpendingPolicy(**kw), Ledger())


def test_per_payment_and_daily_limits():
    e = engine(max_payment_sats=500, daily_limit_sats=1000)
    with pytest.raises(PolicyViolation, match="per-payment"):
        e.check(501, "p")
    e.check(500, "p")
    e.ledger.begin_payment("out", "h1", "p", "s", 500, reserved_fee_sats=5)
    e.ledger.begin_payment("out", "h2", "p", "s", 490)
    with pytest.raises(PolicyViolation, match="daily"):
        e.check(10, "p")
    e.ledger.fail_payment("out", "h2")  # failed payments don't count
    e.check(10, "p")


def test_per_counterparty_limit_allowlist_blocklist_and_kill_switch():
    e = engine(
        per_counterparty_daily_sats=100,
        allowed_counterparties={"a", "b"},
        blocked_counterparties={"b"},
    )
    e.ledger.begin_payment("out", "h", "a", "s", 90)
    with pytest.raises(PolicyViolation, match="counterparty"):
        e.check(20, "a")
    with pytest.raises(PolicyViolation, match="blocked"):
        e.check(1, "b")
    with pytest.raises(PolicyViolation, match="allowlist"):
        e.check(1, "c")
    e.freeze()
    with pytest.raises(PolicyViolation, match="frozen"):
        e.check(1, "a")
    e.unfreeze()
    e.check(10, "a")
    assert e.report()["spent_last_24h_sats"] == 90


def test_routing_fee_limit():
    p = SpendingPolicy(max_routing_fee_sats=50, max_routing_fee_ppm=10_000)
    assert p.routing_fee_limit(100) == 1
    assert p.routing_fee_limit(1000) == 10
    assert p.routing_fee_limit(1_000_000) == 50


def test_ledger_prevents_double_payment():
    ledger = Ledger()
    ledger.begin_payment("out", "h", "p", "s", 10)
    with pytest.raises(DuplicatePayment):
        ledger.begin_payment("out", "h", "p", "s", 10)
    ledger.complete_payment("out", "h", "pre", 1)
    with pytest.raises(DuplicatePayment):
        ledger.begin_payment("out", "h", "p", "s", 10)
    ledger.begin_payment("out", "h2", "p", "s", 10)
    ledger.fail_payment("out", "h2")
    ledger.begin_payment("out", "h2", "p", "s", 10)  # failed payments may be retried
    assert ledger.get_payment("out", "h").status == "succeeded"
    assert ledger.totals()["out_sats"] == 10 and ledger.totals()["out_fee_sats"] == 1


def test_ledger_incoming_and_fees(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    assert ledger.record_incoming("h", "buyer", "s", 100, "pre")
    assert not ledger.record_incoming("h", "buyer", "s", 100, "pre")
    ledger.accrue_fee("h", 1)
    ledger.accrue_fee("h", 1)
    ledger.accrue_fee("h0", 0)
    assert ledger.accrued_fees() == (1, ["h"])
    ledger.mark_fees_settled(["h"], "settle")
    assert ledger.fee_totals() == {"accrued": 0, "settled": 1}
    ledger.record_negotiation("n", "buyer", "p", "s", "open")
    ledger.record_negotiation("n", "buyer", "p", "s", "agreed", 100, 3)
    assert ledger.negotiation_stats() == {"agreed": 1}
    ledger.close()
