"""Use case: build the MarketSnapshot the brain will look at."""

from __future__ import annotations

import logging
from decimal import Decimal

from trader.domain.indicators import compute_indicators
from trader.domain.models import MarketSnapshot, utcnow
from trader.ports import BrokerPort, MarketDataPort

log = logging.getLogger(__name__)


def take_snapshot(
    broker: BrokerPort,
    data: MarketDataPort,
    universe: tuple[str, ...],
    history_days: int,
    capital_cap: Decimal | None = None,
) -> MarketSnapshot:
    """Collect account + quotes + indicators for the universe and any held symbols."""
    account = broker.get_account()
    held = tuple(p.symbol for p in account.positions)
    symbols = sorted(set(universe) | set(held))

    quotes = data.get_quotes(symbols)
    bars = data.get_daily_bars(symbols, history_days)
    indicators = {}
    for sym in symbols:
        ind = compute_indicators(bars.get(sym, []))
        if ind is not None:
            indicators[sym] = ind

    snap = MarketSnapshot(
        taken_at=utcnow(),
        market_open=broker.is_market_open(),
        account=account,
        quotes=quotes,
        indicators=indicators,
        universe=universe,
        capital_cap=capital_cap,
    )
    log.info(
        "snapshot: equity=%s cash=%s positions=%d quotes=%d market_open=%s",
        account.equity,
        account.cash,
        len(account.positions),
        len(quotes),
        snap.market_open,
    )
    return snap
