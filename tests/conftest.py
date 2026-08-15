"""Shared test fixtures. Everything runs offline against fakes."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trader.adapters.fake_broker import FakeBroker, position
from trader.domain.models import Indicators, MarketSnapshot, Quote, RiskConfig


@pytest.fixture
def risk_config() -> RiskConfig:
    return RiskConfig(
        max_position_pct=Decimal("0.25"),
        max_order_notional=Decimal("400"),
        max_orders_per_proposal=3,
        min_cash_buffer_pct=Decimal("0.10"),
        max_daily_loss_pct=Decimal("0.03"),
        max_limit_distance_pct=Decimal("0.03"),
        allow_fractional=True,
        allow_shorting=False,
        blocked_symbols=frozenset({"TQQQ"}),
    )


@pytest.fixture
def broker() -> FakeBroker:
    """$1,000 account holding 2 AAPL at $200; NVDA at $180, SPY at $500."""
    return FakeBroker(
        equity=Decimal("1000"),
        cash=Decimal("600"),
        last_equity=Decimal("1000"),
        positions=(position("AAPL", "2", "200"),),
        prices={"AAPL": Decimal("200"), "NVDA": Decimal("180"), "SPY": Decimal("500"), "TQQQ": Decimal("50")},
    )


def make_snapshot(broker: FakeBroker, universe: tuple[str, ...] = ("AAPL", "NVDA", "SPY", "TQQQ")) -> MarketSnapshot:
    quotes = {s: Quote(s, p, p, Decimal("0")) for s, p in broker.prices.items()}
    return MarketSnapshot(
        taken_at=datetime(2026, 8, 17, 12, 30, tzinfo=UTC),
        market_open=True,
        account=broker.account,
        quotes=quotes,
        indicators={s: Indicators(p, p, p, p, p, Decimal("0"), Decimal("0"), 1000) for s, p in broker.prices.items()},
        universe=universe,
    )


@pytest.fixture
def snapshot(broker: FakeBroker) -> MarketSnapshot:
    return make_snapshot(broker)
