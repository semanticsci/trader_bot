"""Tiny, explainable technical indicators.

These are deliberately simple. The point is not to be clever — it is to give the
brain (and the human) a compact, honest picture of the trend so it doesn't have
to reason over 60 raw numbers. Every function here is pure and unit-tested.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from trader.domain.models import Bar, Indicators

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


def compute_indicators(bars: list[Bar]) -> Indicators | None:
    """Turn a chronological list of daily bars into an ``Indicators`` snapshot."""
    if not bars:
        return None
    closes = [b.close for b in bars]
    last = closes[-1]
    last20 = bars[-20:] if len(bars) >= 20 else None
    return Indicators(
        last_close=last,
        sma_20=sma(closes, 20),
        sma_50=sma(closes, 50),
        high_20d=max(b.high for b in last20) if last20 else None,
        low_20d=min(b.low for b in last20) if last20 else None,
        return_5d_pct=pct_change(last, closes[-6]) if len(closes) >= 6 else None,
        return_20d_pct=pct_change(last, closes[-21]) if len(closes) >= 21 else None,
        avg_volume_20d=(sum(b.volume for b in last20) // 20) if last20 else None,
    )
