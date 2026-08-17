"""The risk gate is the safety net. Every rule gets a test that shows it catching something."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from trader.adapters.fake_broker import FakeBroker, order, position
from trader.domain import risk
from trader.domain.models import Side

from .conftest import make_snapshot


def _reasons(results, i=0):
    return " ".join(results[i].reasons)


def test_reasonable_buy_passes(snapshot, risk_config):
    # Arrange: buy 1 NVDA at $180 in a $1000 account with $600 cash.
    o = order("NVDA", Side.BUY, "1", "180")
    # Act
    res = risk.evaluate([o], snapshot, risk_config)
    # Assert
    assert res[0].accepted, res[0].reasons


def test_kill_switch_rejects_everything(snapshot, risk_config):
    res = risk.evaluate([order("NVDA", Side.BUY, "1", "180")], snapshot, risk_config, halted=True)
    assert not res[0].accepted
    assert "kill switch" in _reasons(res)


def test_blocked_symbol(snapshot, risk_config):
    res = risk.evaluate([order("TQQQ", Side.BUY, "1", "50")], snapshot, risk_config)
    assert "blocked" in _reasons(res)


def test_symbol_outside_universe(snapshot, risk_config):
    snap = replace(snapshot, universe=("AAPL",))  # NVDA no longer allowed
    res = risk.evaluate([order("NVDA", Side.BUY, "1", "180")], snap, risk_config)
    assert "not in the configured universe" in _reasons(res)


def test_held_symbol_outside_universe_can_still_be_sold(snapshot, risk_config):
    snap = replace(snapshot, universe=("NVDA",))  # AAPL not in universe but held
    res = risk.evaluate([order("AAPL", Side.SELL, "1", "200")], snap, risk_config)
    assert res[0].accepted, res[0].reasons


def test_limit_far_from_price_is_rejected(snapshot, risk_config):
    res = risk.evaluate([order("NVDA", Side.BUY, "1", "150")], snapshot, risk_config)  # 16% below
    assert "looks like a mistake" in _reasons(res)


def test_order_notional_cap(snapshot, risk_config):
    # 3 SPY @ 500 = $1500 > $400 cap (and > cash, and > position cap — several reasons)
    res = risk.evaluate([order("SPY", Side.BUY, "3", "500")], snapshot, risk_config)
    assert "max single order" in _reasons(res)


def test_position_concentration_cap(snapshot, risk_config):
    # Already hold $400 of AAPL (40% — over cap already); buying more must be rejected.
    res = risk.evaluate([order("AAPL", Side.BUY, "1", "200")], snapshot, risk_config)
    assert "of capital, cap is" in _reasons(res)


def test_cash_buffer(risk_config):
    # Equity 1000, cash 400, buffer 10% = $100 → at most $300 of buys this proposal.
    # Each order alone is under the 25% concentration cap; together they breach the cash buffer.
    b = FakeBroker(
        equity=Decimal("1000"), cash=Decimal("400"), last_equity=Decimal("1000"),
        positions=(position("AAPL", "3", "200"),),
        prices={"AAPL": Decimal("200"), "NVDA": Decimal("180"), "SPY": Decimal("500")},
    )
    snap = make_snapshot(b, universe=("AAPL", "NVDA", "SPY"))
    orders = [order("NVDA", Side.BUY, "1.3", "180"), order("SPY", Side.BUY, "0.5", "500")]  # $234 + $250
    res = risk.evaluate(orders, snap, risk_config)
    assert res[0].accepted, res[0].reasons
    assert not res[1].accepted
    assert "buffer" in _reasons(res, 1)


def test_max_orders_per_proposal(snapshot, risk_config):
    cfg = replace(risk_config, max_orders_per_proposal=1, max_order_notional=Decimal("1000"))
    orders = [order("NVDA", Side.BUY, "1", "180"), order("SPY", Side.BUY, "0.2", "500")]
    res = risk.evaluate(orders, snapshot, cfg)
    assert res[0].accepted
    assert "more than 1 orders" in _reasons(res, 1)


def test_no_shorting(snapshot, risk_config):
    res = risk.evaluate([order("NVDA", Side.SELL, "1", "180")], snapshot, risk_config)
    assert "shorting is disabled" in _reasons(res)


def test_cannot_sell_more_than_held(snapshot, risk_config):
    res = risk.evaluate([order("AAPL", Side.SELL, "3", "200")], snapshot, risk_config)
    assert "would open a short" in _reasons(res)


def test_sells_are_cumulative(snapshot, risk_config):
    # Hold 2 AAPL. Sell 1, then sell 2 → second exceeds what's left.
    orders = [order("AAPL", Side.SELL, "1", "200"), order("AAPL", Side.SELL, "2", "200")]
    res = risk.evaluate(orders, snapshot, risk_config)
    assert res[0].accepted
    assert not res[1].accepted


def test_daily_loss_breaker_blocks_buys_but_allows_sells(risk_config):
    # Down 5% on the day: equity 950 vs last_equity 1000.
    b = FakeBroker(
        equity=Decimal("950"), cash=Decimal("550"), last_equity=Decimal("1000"),
        positions=(position("AAPL", "2", "200"),),
        prices={"AAPL": Decimal("200"), "NVDA": Decimal("180")},
    )
    snap = make_snapshot(b, universe=("AAPL", "NVDA"))
    res = risk.evaluate([order("NVDA", Side.BUY, "1", "180"), order("AAPL", Side.SELL, "1", "200")], snap, risk_config)
    assert not res[0].accepted and "breaker" in _reasons(res, 0)
    assert res[1].accepted


def test_fractional_disabled(snapshot, risk_config):
    cfg = replace(risk_config, allow_fractional=False)
    res = risk.evaluate([order("NVDA", Side.BUY, "0.5", "180")], snapshot, cfg)
    assert "fractional" in _reasons(res)


def test_missing_rationale_is_rejected(snapshot, risk_config):
    res = risk.evaluate([order("NVDA", Side.BUY, "1", "180", why="  ")], snapshot, risk_config)
    assert "rationale" in _reasons(res)


def test_every_rejection_has_a_reason(snapshot, risk_config):
    bad = [order("TQQQ", Side.BUY, "-1", "0"), order("NVDA", Side.SELL, "1", "10")]
    for r in risk.evaluate(bad, snapshot, risk_config):
        assert not r.accepted
        assert r.reasons and all(r.reasons)


def test_summarize(snapshot, risk_config):
    res = risk.evaluate([order("NVDA", Side.BUY, "1", "180"), order("TQQQ", Side.BUY, "1", "50")], snapshot, risk_config)
    assert risk.summarize(res) == "1 of 2 passed the risk gate"


# ---------------------------------------------------------------- capital cap (paper $100k acting like $1,000)


def _big_paper_account(prices=None):
    return FakeBroker(
        equity=Decimal("100000"), cash=Decimal("100000"), last_equity=Decimal("100000"),
        prices=prices or {"NVDA": Decimal("180"), "SPY": Decimal("500"), "AAPL": Decimal("200")},
    )


def test_capital_cap_measures_position_pct_against_cap(risk_config):
    """$100k account, $1,000 cap: a $360 buy is 36% of the *cap* → rejected, even though it's 0.4% of equity."""
    cfg = replace(risk_config, capital_cap=Decimal("1000"))
    snap = make_snapshot(_big_paper_account(), universe=("NVDA", "SPY", "AAPL"))
    res = risk.evaluate([order("NVDA", Side.BUY, "2", "180")], snap, cfg)
    assert not res[0].accepted and "of capital, cap is 25%" in _reasons(res)


def test_capital_cap_limits_total_deployed(risk_config):
    """Three $300 buys pass individually ($900); the fourth would push deployed capital to $1,200."""
    cfg = replace(risk_config, capital_cap=Decimal("1000"), max_orders_per_proposal=10, max_position_pct=Decimal("0.5"))
    b = _big_paper_account({"NVDA": Decimal("300"), "SPY": Decimal("300"), "AAPL": Decimal("300"), "MSFT": Decimal("300")})
    snap = make_snapshot(b, universe=("NVDA", "SPY", "AAPL", "MSFT"))
    orders = [order(s, Side.BUY, "1", "300") for s in ("NVDA", "SPY", "AAPL", "MSFT")]
    res = risk.evaluate(orders, snap, cfg)
    assert [r.accepted for r in res] == [True, True, True, False]
    assert "capital cap is $1000" in _reasons(res, 3)


def test_capital_cap_counts_existing_positions(risk_config):
    """Already holding $900 → only $100 more may be deployed."""
    cfg = replace(risk_config, capital_cap=Decimal("1000"))
    b = FakeBroker(
        equity=Decimal("100000"), cash=Decimal("99100"), last_equity=Decimal("100000"),
        positions=(position("SPY", "1.8", "500"),),
        prices={"SPY": Decimal("500"), "NVDA": Decimal("180")},
    )
    snap = make_snapshot(b, universe=("SPY", "NVDA"))
    res = risk.evaluate([order("NVDA", Side.BUY, "1", "180")], snap, cfg)  # $180 > $100 headroom
    assert not res[0].accepted and "capital cap" in _reasons(res)


def test_no_cap_uses_full_equity(risk_config):
    cfg = replace(risk_config, capital_cap=None)
    snap = make_snapshot(_big_paper_account(), universe=("NVDA", "SPY", "AAPL"))
    res = risk.evaluate([order("NVDA", Side.BUY, "2", "180")], snap, cfg)  # $360 on $100k: fine
    assert res[0].accepted, res[0].reasons


# ---------------------------------------------------------------- open (unfilled) orders count as committed capital


def _pending(symbol, side, qty, price):
    from trader.domain.models import BrokerOrder
    return BrokerOrder(f"open-{symbol}", symbol, side, Decimal(qty), Decimal(price), "accepted")


def test_pending_buys_count_toward_capital_cap(risk_config):
    """$590 of unfilled buys at the broker + a new $450 buy would exceed the $1,000 cap."""
    cfg = replace(risk_config, capital_cap=Decimal("1000"), max_order_notional=Decimal("500"), max_position_pct=Decimal("0.5"))
    b = _big_paper_account({"NVDA": Decimal("225"), "MSFT": Decimal("495"), "SPY": Decimal("776"), "QQQ": Decimal("731")})
    snap = replace(make_snapshot(b, universe=("NVDA", "MSFT", "SPY", "QQQ")),
                   open_orders=(_pending("NVDA", Side.BUY, "0.89", "224.03"), _pending("MSFT", Side.BUY, "0.4", "492.87"),
                                _pending("SPY", Side.BUY, "0.25", "772.15")))
    res = risk.evaluate([order("QQQ", Side.BUY, "0.6", "731")], snap, cfg)  # $438.6 → total $1,028
    assert not res[0].accepted and "capital cap" in _reasons(res)
    res_ok = risk.evaluate([order("QQQ", Side.BUY, "0.5", "731")], snap, cfg)  # $365.5 → total $955
    assert res_ok[0].accepted, res_ok[0].reasons


def test_pending_buy_counts_toward_position_concentration(risk_config):
    cfg = replace(risk_config, capital_cap=Decimal("1000"))
    b = _big_paper_account({"NVDA": Decimal("225")})
    snap = replace(make_snapshot(b, universe=("NVDA",)), open_orders=(_pending("NVDA", Side.BUY, "0.89", "224.03"),))
    res = risk.evaluate([order("NVDA", Side.BUY, "0.5", "225")], snap, cfg)  # 199 + 112 = 31% of cap
    assert not res[0].accepted and "of capital, cap is 25%" in _reasons(res)


def test_pending_sell_reduces_sellable_qty(risk_config):
    b = FakeBroker(equity=Decimal("1000"), cash=Decimal("600"), positions=(position("AAPL", "2", "200"),),
                   prices={"AAPL": Decimal("200")})
    snap = replace(make_snapshot(b, universe=("AAPL",)), open_orders=(_pending("AAPL", Side.SELL, "1.5", "201"),))
    res = risk.evaluate([order("AAPL", Side.SELL, "1", "200")], snap, risk_config)  # only 0.5 left to sell
    assert not res[0].accepted and "would open a short" in _reasons(res)


# ---------------------------------------------------------------- rotation: sells in the same proposal free capital


def test_sell_then_buy_rotation_passes_when_book_is_full(risk_config):
    """Fully deployed ($1,000 cap). Sell $200 of A, buy $200 of B in the same proposal → both pass.
    Buy first, sell later → the buy is rejected (order matters; the brain lists sells first)."""
    cfg = replace(risk_config, capital_cap=Decimal("1000"), min_cash_buffer_pct=Decimal("0"), max_orders_per_proposal=5)
    b = FakeBroker(
        equity=Decimal("100000"), cash=Decimal("99000"), last_equity=Decimal("100000"),
        positions=(position("AAA", "2", "200"), position("BBB", "3", "200")),  # $400 + $600 = $1,000 deployed
        prices={"AAA": Decimal("200"), "BBB": Decimal("200"), "CCC": Decimal("100")},
    )
    snap = make_snapshot(b, universe=("AAA", "BBB", "CCC"))
    good = risk.evaluate([order("AAA", Side.SELL, "1", "200"), order("CCC", Side.BUY, "2", "100")], snap, cfg)
    assert [r.accepted for r in good] == [True, True], [r.reasons for r in good]
    bad = risk.evaluate([order("CCC", Side.BUY, "2", "100"), order("AAA", Side.SELL, "1", "200")], snap, cfg)
    assert not bad[0].accepted and "capital cap" in _reasons(bad, 0)
