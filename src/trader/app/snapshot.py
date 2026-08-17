"""Use case: build the MarketSnapshot the brain will look at.

The wide view: quotes + indicators for the whole universe, SPY-relative strength, a market
regime read, a momentum-ranked shortlist, and recent headlines for held names and leaders.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from trader.domain.indicators import compute_indicators, market_regime, rank_universe
from trader.domain.models import MarketSnapshot, utcnow
from trader.ports import BrokerPort, MarketDataPort

log = logging.getLogger(__name__)

BENCHMARK = "SPY"


def take_snapshot(
    broker: BrokerPort,
    data: MarketDataPort,
    universe: tuple[str, ...],
    history_days: int,
    capital_cap: Decimal | None = None,
    *,
    with_news: bool = True,
) -> MarketSnapshot:
    """Collect account + quotes + indicators for the universe and any held/pending symbols."""
    account = broker.get_account()
    open_orders = tuple(broker.get_open_orders())
    held = tuple(p.symbol for p in account.positions)
    pending = tuple(o.symbol for o in open_orders)
    symbols = sorted(set(universe) | set(held) | set(pending) | {BENCHMARK})

    quotes = data.get_quotes(symbols)
    bars = data.get_daily_bars(symbols, history_days)

    # SPY first, so every other name can be measured against it.
    spy_ind = compute_indicators(bars.get(BENCHMARK, []), quote=quotes.get(BENCHMARK))
    spy_r20 = spy_ind.return_20d_pct if spy_ind else None
    indicators = {}
    for sym in symbols:
        ind = compute_indicators(bars.get(sym, []), quote=quotes.get(sym), spy_return_20d=spy_r20)
        if ind is not None:
            indicators[sym] = ind

    ranking = rank_universe(indicators, top=12, bottom=5)
    regime = market_regime(indicators, BENCHMARK)

    news: dict[str, tuple[str, ...]] = {}
    if with_news:
        want = list(dict.fromkeys(list(held) + [r["symbol"] for r in ranking if r["tag"] == "leader"]))[:20]
        raw = data.get_news(want, limit=40) if want else {}
        news = {s: tuple(v) for s, v in raw.items()}

    snap = MarketSnapshot(
        taken_at=utcnow(),
        market_open=broker.is_market_open(),
        account=account,
        quotes=quotes,
        indicators=indicators,
        universe=universe,
        capital_cap=capital_cap,
        open_orders=open_orders,
        regime=regime,
        ranking=ranking,
        news=news,
    )
    log.info(
        "snapshot: equity=%s cash=%s positions=%d open_orders=%d symbols=%d regime=%s breadth=%s market_open=%s",
        account.equity, account.cash, len(account.positions), len(open_orders), len(indicators),
        regime.get("verdict"), regime.get("breadth_above_sma20"), snap.market_open,
    )
    return snap
