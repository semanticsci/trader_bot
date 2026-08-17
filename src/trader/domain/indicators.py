"""Explainable technical indicators, plus the wide-view helpers (ranking, regime).

Every function is pure and unit-tested. Nothing here is magic; each number exists
so the brain (and you) can see, in one line per symbol, whether a name is
trending, stretched, volatile, leading or lagging the market — without reading
60 raw bars.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from trader.domain.models import Bar, Indicators, Quote

_CENTS = Decimal("0.01")
_PCT = Decimal("0.0001")


def sma(values: list[Decimal], window: int) -> Decimal | None:
    """Simple moving average of the last ``window`` values, or None if not enough data."""
    if len(values) < window or window <= 0:
        return None
    return (sum(values[-window:]) / Decimal(window)).quantize(_CENTS, rounding=ROUND_HALF_UP)


def pct_change(new: Decimal, old: Decimal) -> Decimal | None:
    """(new - old) / old as a fraction, e.g. 0.0521 for +5.21%."""
    if old == 0:
        return None
    return ((new - old) / old).quantize(_PCT, rounding=ROUND_HALF_UP)


def rsi(closes: list[Decimal], period: int = 14) -> Decimal | None:
    """Wilder's RSI on closes. >70 = stretched, <30 = washed out. None if not enough data."""
    if len(closes) < period + 1:
        return None
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for prev, cur in zip(closes[:-1], closes[1:], strict=False):
        d = cur - prev
        gains.append(max(d, Decimal("0")))
        losses.append(max(-d, Decimal("0")))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, lo in zip(gains[period:], losses[period:], strict=False):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + lo) / period
    if avg_loss == 0:
        return Decimal("100")
    rs = avg_gain / avg_loss
    return (Decimal("100") - Decimal("100") / (1 + rs)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def atr_pct(bars: list[Bar], period: int = 14) -> Decimal | None:
    """Average True Range over ``period`` bars, expressed as a fraction of the last close.

    0.02 means the name typically moves ~2% of its price in a day. Use it to size stops:
    a -3% stop on a 1%-a-day stock is generous; on a 5%-a-day stock it is noise.
    """
    if len(bars) < period + 1:
        return None
    trs: list[Decimal] = []
    for prev, cur in zip(bars[-period - 1 : -1], bars[-period:], strict=False):
        tr = max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        trs.append(tr)
    last = bars[-1].close
    if last == 0:
        return None
    return (sum(trs) / period / last).quantize(_PCT, rounding=ROUND_HALF_UP)


def compute_indicators(
    bars: list[Bar],
    *,
    quote: Quote | None = None,
    spy_return_20d: Decimal | None = None,
) -> Indicators | None:
    """Turn a chronological list of daily bars (+ optional live quote) into an ``Indicators``.

    Args:
        bars: oldest first. The last bar may be today's partial bar (intraday).
        quote: the live quote, if any — used for gap / range position / distance to high.
        spy_return_20d: SPY's 20-day return, for relative strength.
    """
    if not bars:
        return None
    closes = [b.close for b in bars]
    last = closes[-1]
    price = quote.price if quote and quote.price > 0 else last
    last20 = bars[-20:] if len(bars) >= 20 else None
    high_20d = max(b.high for b in last20) if last20 else None
    ret_20 = pct_change(last, closes[-21]) if len(closes) >= 21 else None

    # Today's bar (the last one) vs yesterday's close, for gap / range / volume ratio.
    today = bars[-1]
    yday_close = closes[-2] if len(closes) >= 2 else None
    gap = pct_change(today.open, yday_close) if yday_close else None
    rng = today.high - today.low
    range_pos = ((price - today.low) / rng).quantize(_PCT) if rng > 0 else None
    avg_vol = (sum(b.volume for b in bars[-21:-1]) // 20) if len(bars) >= 21 else None
    vol_ratio = (Decimal(today.volume) / Decimal(avg_vol)).quantize(_PCT) if avg_vol else None

    return Indicators(
        last_close=last,
        sma_20=sma(closes, 20),
        sma_50=sma(closes, 50),
        high_20d=high_20d,
        low_20d=min(b.low for b in last20) if last20 else None,
        return_5d_pct=pct_change(last, closes[-6]) if len(closes) >= 6 else None,
        return_20d_pct=ret_20,
        avg_volume_20d=(sum(b.volume for b in last20) // 20) if last20 else None,
        rsi_14=rsi(closes),
        atr_pct_14=atr_pct(bars),
        rs_20d_vs_spy=(ret_20 - spy_return_20d) if (ret_20 is not None and spy_return_20d is not None) else None,
        gap_pct=gap,
        range_pos=range_pos,
        volume_ratio=vol_ratio,
        dist_to_high_20d_pct=pct_change(price, high_20d) if high_20d else None,
    )


# --------------------------------------------------------------------------- wide view


def momentum_score(ind: Indicators) -> Decimal | None:
    """One transparent number to sort the universe by.

    score = 20d return + 5d return + relative strength vs SPY − (distance below 20d high)
    Higher = stronger, nearer its highs, leading the market. It's a *shortlist* heuristic,
    not a signal — the brain still reads the rows.
    """
    if ind.return_20d_pct is None or ind.return_5d_pct is None:
        return None
    s = ind.return_20d_pct + ind.return_5d_pct
    if ind.rs_20d_vs_spy is not None:
        s += ind.rs_20d_vs_spy
    if ind.dist_to_high_20d_pct is not None:
        s += ind.dist_to_high_20d_pct  # negative number: further below the high = worse
    return s.quantize(_PCT)


def rank_universe(indicators: dict[str, Indicators], top: int = 12, bottom: int = 5) -> tuple[dict[str, Any], ...]:
    """Score every symbol and return the strongest ``top`` and weakest ``bottom`` as compact rows."""
    scored = []
    for sym, ind in indicators.items():
        s = momentum_score(ind)
        if s is None:
            continue
        scored.append((s, sym, ind))
    scored.sort(key=lambda t: -t[0])
    rows = []
    for tag, chunk in (("leader", scored[:top]), ("laggard", scored[-bottom:] if len(scored) > top else [])):
        for s, sym, ind in chunk:
            rows.append(
                {
                    "symbol": sym,
                    "tag": tag,
                    "score": str(s),
                    "ret_5d": str(ind.return_5d_pct),
                    "ret_20d": str(ind.return_20d_pct),
                    "rs_20d_vs_spy": str(ind.rs_20d_vs_spy) if ind.rs_20d_vs_spy is not None else None,
                    "dist_to_high_20d": str(ind.dist_to_high_20d_pct) if ind.dist_to_high_20d_pct is not None else None,
                    "rsi_14": str(ind.rsi_14) if ind.rsi_14 is not None else None,
                    "atr_pct_14": str(ind.atr_pct_14) if ind.atr_pct_14 is not None else None,
                    "above_sma20": bool(ind.sma_20 and ind.last_close > ind.sma_20),
                    "above_sma50": bool(ind.sma_50 and ind.last_close > ind.sma_50),
                }
            )
    return tuple(rows)


def market_regime(indicators: dict[str, Indicators], benchmark: str = "SPY") -> dict[str, Any]:
    """A plain-English read of the tape: is this a market to be long in?

    Combines the benchmark's trend with breadth (share of the universe above its 20-day SMA).
    The verdict is deliberately coarse: risk_on / neutral / risk_off.
    """
    spy = indicators.get(benchmark)
    total = [i for i in indicators.values() if i.sma_20 is not None]
    above20 = sum(1 for i in total if i.last_close > i.sma_20)  # type: ignore[operator]
    breadth = (Decimal(above20) / Decimal(len(total))).quantize(_PCT) if total else None
    spy_above_20 = bool(spy and spy.sma_20 and spy.last_close > spy.sma_20)
    spy_above_50 = bool(spy and spy.sma_50 and spy.last_close > spy.sma_50)
    spy_5d = spy.return_5d_pct if spy else None
    if spy_above_20 and spy_above_50 and (breadth is None or breadth >= Decimal("0.5")):
        verdict = "risk_on"
    elif not spy_above_50 or (breadth is not None and breadth < Decimal("0.35")):
        verdict = "risk_off"
    else:
        verdict = "neutral"
    return {
        "verdict": verdict,
        "benchmark": benchmark,
        "spy_above_sma20": spy_above_20,
        "spy_above_sma50": spy_above_50,
        "spy_return_5d": str(spy_5d) if spy_5d is not None else None,
        "spy_rsi_14": str(spy.rsi_14) if spy and spy.rsi_14 is not None else None,
        "breadth_above_sma20": str(breadth) if breadth is not None else None,
        "universe_size": len(total),
        "explain": (
            "risk_on = SPY above 20d & 50d and >=50% of universe above 20d; "
            "risk_off = SPY below 50d or breadth <35%; else neutral"
        ),
    }
