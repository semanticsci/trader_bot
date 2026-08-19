"""Use case: build the MarketSnapshot the brain will look at.

The wide view: quotes + indicators for the whole universe, SPY-relative strength, a market
regime read, a momentum-ranked shortlist, and recent headlines for held names and leaders.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from trader.domain.indicators import compute_indicators, market_regime, rank_universe
from trader.domain.models import MarketSnapshot, Money, utcnow
from trader.domain.sectors import sector_of
from trader.ports import BrokerPort, EventsPort, MarketDataPort

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
    events: EventsPort | None = None,
    inception: tuple[datetime, Money] | None = None,
    last_decision: dict[str, Any] | None = None,
) -> MarketSnapshot:
    """Collect account + quotes + indicators for the universe and any held/pending symbols.

    Args:
        events: optional earnings-date source (held names + shortlist are looked up).
        inception: (when the book started, equity then) — enables the since-inception P&L in ``book``.
        last_decision: a short dict describing the previous proposal (summary, orders, outcome).
    """
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

    ranking = tuple({**r, "sector": sector_of(r["symbol"])} for r in rank_universe(indicators, top=12, bottom=5))
    regime = market_regime(indicators, BENCHMARK)
    focus = list(dict.fromkeys(list(held) + [r["symbol"] for r in ranking]))

    news: dict[str, tuple[str, ...]] = {}
    if with_news:
        want = list(dict.fromkeys(list(held) + [r["symbol"] for r in ranking if r["tag"] == "leader"]))[:20]
        raw = data.get_news(want, limit=40) if want else {}
        news = {s: tuple(v) for s, v in raw.items()}

    ev: dict[str, Any] = {}
    if events is not None and focus:
        try:
            today = date.today()  # noqa: DTZ011 — calendar dates
            for sym, iso in events.next_earnings(focus[:25]).items():
                days = (date.fromisoformat(iso) - today).days
                ev[sym] = {"next_earnings": iso, "days_away": days, "held": sym in held}
        except Exception as exc:  # noqa: BLE001 — events are nice-to-have; a snapshot must never fail on them
            log.warning("earnings lookup failed: %s", type(exc).__name__)

    book = _book_view(broker, account, held, capital_cap, inception, last_decision)

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
        events=ev,
        book=book,
    )
    log.info(
        "snapshot: equity=%s cash=%s positions=%d open_orders=%d symbols=%d regime=%s breadth=%s market_open=%s",
        account.equity, account.cash, len(account.positions), len(open_orders), len(indicators),
        regime.get("verdict"), regime.get("breadth_above_sma20"), snap.market_open,
    )
    return snap


def _book_view(
    broker: BrokerPort,
    account: Any,
    held: tuple[str, ...],
    capital_cap: Money | None,
    inception: tuple[datetime, Money] | None,
    last_decision: dict[str, Any] | None,
) -> dict[str, Any]:
    """What the brain needs to know about *its own* recent behaviour. Never raises."""
    now = utcnow()
    out: dict[str, Any] = {
        "holdings": [
            {"symbol": p.symbol, "qty": str(p.qty), "market_value": str(p.market_value),
             "unrealized_pl_pct": f"{(p.unrealized_plpc * 100):.2f}%", "sector": sector_of(p.symbol)}
            for p in account.positions
        ],
        "sector_exposure": {},
        "recent_fills_7d": [],
    }
    invested = sum((p.market_value for p in account.positions), Decimal("0"))
    if invested > 0:
        by_sector: dict[str, Decimal] = {}
        for p in account.positions:
            by_sector[sector_of(p.symbol)] = by_sector.get(sector_of(p.symbol), Decimal("0")) + p.market_value
        base = capital_cap or invested
        out["sector_exposure"] = {k: f"{(v / base * 100):.1f}%" for k, v in sorted(by_sector.items())}
    if inception is not None:
        since_at, since_eq = inception
        pnl = account.equity - since_eq
        out["since_inception"] = {
            "started": since_at.isoformat(),
            "days": (now - since_at).days,
            "pnl_dollars": str(pnl.quantize(Decimal("0.01"))),
            "pnl_pct_of_cap": f"{(pnl / capital_cap * 100):.2f}%" if capital_cap else None,
        }
    try:
        fills = [
            o for o in broker.get_orders_since(now - timedelta(days=7))
            if o.filled_qty > 0 and o.filled_at is not None
        ]
        fills.sort(key=lambda o: o.filled_at or now)
        out["recent_fills_7d"] = [
            {"when": (o.filled_at or now).isoformat(timespec="minutes"), "side": o.side.value, "symbol": o.symbol,
             "qty": str(o.filled_qty.normalize()), "price": str(o.filled_avg_price)}
            for o in fills[-25:]
        ]
    except Exception as exc:  # noqa: BLE001 — history is context, not a hard dependency
        log.warning("recent fills unavailable: %s", type(exc).__name__)
    if last_decision:
        out["last_decision"] = last_decision
    return out
