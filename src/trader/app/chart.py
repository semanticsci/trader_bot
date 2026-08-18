"""Use case: the performance picture — a 7-day chart plus the headline numbers.

Why not just use the broker's chart? Because the paper account is $100k and the book is
$1,000: a 5% week on the book is a 0.05% wiggle on the broker's line. So everything here is
measured as **P&L in dollars, and as a % of the capital budget** — since inception, this
week, this month, today — with "what if I'd just held SPY with the same $1,000" as the
honest benchmark.

``build_performance`` is pure (testable); ``render_chart`` draws; the CLI sends it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trader.domain.models import Bar, BrokerOrder, Side, utcnow

Point = tuple[datetime, Decimal]


@dataclass(frozen=True)
class Performance:
    """Everything the chart and the caption need."""

    inception: datetime
    inception_equity: Decimal
    capital_cap: Decimal
    now_equity: Decimal
    # series for the chart window (oldest first)
    pnl_series: tuple[Point, ...]  # (ts, equity - inception_equity)
    spy_series: tuple[Point, ...]  # (ts, cap * (spy/spy_at_inception - 1)) — same-dollars benchmark
    fills: tuple[tuple[datetime, str, Side, Decimal], ...] = ()  # (ts, symbol, side, pnl_at_that_time)
    # headline numbers, in dollars
    since_inception: Decimal = Decimal("0")
    week: Decimal = Decimal("0")
    month: Decimal = Decimal("0")
    today: Decimal = Decimal("0")
    spy_since_inception: Decimal = Decimal("0")
    warnings: tuple[str, ...] = field(default=())

    def pct(self, dollars: Decimal) -> Decimal:
        return (dollars / self.capital_cap * 100) if self.capital_cap else Decimal("0")


def build_performance(
    history: list[Point],
    *,
    inception: datetime,
    inception_equity: Decimal,
    capital_cap: Decimal,
    spy_bars: list[Bar],
    fills: list[BrokerOrder],
    now: datetime | None = None,
    window_days: int = 7,
    spy_now: Decimal | None = None,
    prev_close_equity: Decimal | None = None,
) -> Performance:
    """Turn raw equity history + SPY bars into P&L series and headline numbers.

    Args:
        history: (ts, equity) points from the broker, oldest first.
        inception: when the book started (first proposal). Everything is measured from here.
        inception_equity: account equity at inception; P&L = equity - this.
        capital_cap: the budget; percentages are of this number.
        spy_bars: daily SPY bars covering the window (for the benchmark line).
        fills: broker orders (filled ones are marked on the chart).
        now: injectable clock for tests.
        window_days: how many days the chart shows.
    """
    now = now or utcnow()
    warnings: list[str] = []
    pts = sorted((ts, eq) for ts, eq in history if eq > 0)
    if not pts:
        warnings.append("no equity history from broker yet")
        now_equity = inception_equity
    else:
        now_equity = pts[-1][1]

    window_start = now - timedelta(days=window_days)
    in_window = [(ts, eq - inception_equity) for ts, eq in pts if ts >= window_start and ts > inception]
    if inception >= window_start:
        in_window.insert(0, (inception, Decimal("0")))  # the book starts at zero P&L, by definition
    elif pts:
        prior = [(ts, eq) for ts, eq in pts if ts <= window_start]
        if prior:
            in_window.insert(0, (window_start, prior[-1][1] - inception_equity))

    def equity_at(when: datetime) -> Decimal | None:
        """Last known equity at or before `when` (None if none)."""
        prior = [eq for ts, eq in pts if ts <= when]
        return prior[-1] if prior else None

    since = now_equity - inception_equity
    week_base = equity_at(now - timedelta(days=7)) or inception_equity
    month_base = equity_at(now - timedelta(days=30)) or inception_equity
    # "Today" is measured from the broker's previous-close equity when we have it (Alpaca's
    # last_equity); the midnight lookup is only a fallback.
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_base = prev_close_equity or equity_at(midnight) or inception_equity
    # A brand-new book: week/month bases can't be earlier than inception.
    if now - inception < timedelta(days=7):
        week_base = inception_equity
    if now - inception < timedelta(days=30):
        month_base = inception_equity

    # SPY benchmark: same dollars, bought at inception, held.
    spy_series: list[Point] = []
    spy_since = Decimal("0")
    if spy_bars:
        # Benchmark starts at inception: "bought SPY with the same dollars at the last close on or
        # before inception, held since". Bars before inception are not drawn.
        before = [b for b in spy_bars if datetime.fromisoformat(b.date).date() <= inception.date()]
        base_close = (before[-1] if before else spy_bars[0]).close
        spy_series.append((inception, Decimal("0")))
        first_after = True
        for b in spy_bars:
            ts = _close_ts(b.date)
            if ts > inception and ts >= window_start - timedelta(days=1):
                if first_after:  # flat until the first session after inception opens, then the open print
                    spy_series.append((ts - timedelta(hours=6, minutes=30), capital_cap * (b.open / base_close - 1)))
                    first_after = False
                spy_series.append((ts, capital_cap * (b.close / base_close - 1)))
        last_spy = spy_now if spy_now is not None else spy_bars[-1].close
        if spy_now is not None:
            spy_series.append((now, capital_cap * (spy_now / base_close - 1)))
        spy_since = capital_cap * (last_spy / base_close - 1)
    else:
        warnings.append("no SPY bars for benchmark")

    # Until the first fill the book is flat at zero by definition — draw it that way instead of
    # interpolating a slope through the weekend.
    first_fill = min((o.filled_at for o in fills if o.filled_at and o.filled_qty > 0), default=None)
    if first_fill and inception < first_fill and not any(ts >= first_fill for ts, _ in in_window[:1]):
        in_window = [(ts, v) for ts, v in in_window if ts <= inception or ts >= first_fill]
        in_window.insert(1 if in_window and in_window[0][0] <= inception else 0, (first_fill, Decimal("0")))
        in_window.sort()

    fill_marks: list[tuple[datetime, str, Side, Decimal]] = []
    for o in fills:
        if o.filled_at and o.filled_qty > 0 and o.filled_at >= window_start:
            eq = equity_at(o.filled_at)
            fill_marks.append((o.filled_at, o.symbol, o.side, (eq - inception_equity) if eq else Decimal("0")))

    return Performance(
        inception=inception,
        inception_equity=inception_equity,
        capital_cap=capital_cap,
        now_equity=now_equity,
        pnl_series=tuple(in_window),
        spy_series=tuple(spy_series),
        fills=tuple(fill_marks),
        since_inception=since,
        week=now_equity - week_base,
        month=now_equity - month_base,
        today=now_equity - day_base,
        spy_since_inception=spy_since,
        warnings=tuple(warnings),
    )


def _close_ts(date_str: str) -> datetime:
    """A daily bar's date, stamped at the 16:00 New York close (as an aware UTC datetime)."""
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    d = datetime.fromisoformat(date_str)
    return d.replace(hour=16, minute=0, tzinfo=ny).astimezone(ZoneInfo("UTC"))


def caption(p: Performance, mode: str) -> str:
    """The numbers, for the Telegram caption. Plain text, no spin."""
    days = max((utcnow() - p.inception).days, 0)
    lines = [
        f"📈 Book performance [{mode.upper()}] — day {days} since inception ({p.inception:%b %d})",
        f"Since inception: {_money(p.since_inception)}  ({_pct(p, p.since_inception)} of ${p.capital_cap:,.0f})",
        f"This week:       {_money(p.week)}  ({_pct(p, p.week)})",
        f"This month:      {_money(p.month)}  ({_pct(p, p.month)})",
        f"Today:           {_money(p.today)}  ({_pct(p, p.today)})",
        f"SPY, same $ held since inception: {_money(p.spy_since_inception)}  ({_pct(p, p.spy_since_inception)})",
    ]
    if p.warnings:
        lines.append("note: " + "; ".join(p.warnings))
    return "\n".join(lines)


def render_chart(p: Performance, out_path: Path, *, title: str = "Book P&L — last 7 days") -> Path:
    """Draw the chart to a PNG. Imports matplotlib lazily so the rest of the app never needs it."""
    import matplotlib

    matplotlib.use("Agg")
    from zoneinfo import ZoneInfo

    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    ny = ZoneInfo("America/New_York")
    esc = lambda s: s.replace("$", r"\$")  # noqa: E731 — matplotlib treats $...$ as math mode
    fig, ax = plt.subplots(figsize=(9, 4.8), dpi=150)
    if p.pnl_series:
        xs = [ts.astimezone(ny) for ts, _ in p.pnl_series]
        ys = [float(v) for _, v in p.pnl_series]
        ax.plot(xs, ys, color="#1f77b4", linewidth=2, label=esc("Book P&L ($)"))
        ax.fill_between(xs, ys, 0, where=[y >= 0 for y in ys], color="#1f77b4", alpha=0.10)
        ax.fill_between(xs, ys, 0, where=[y < 0 for y in ys], color="#d62728", alpha=0.10)
    if p.spy_series:
        ax.plot([ts.astimezone(ny) for ts, _ in p.spy_series], [float(v) for _, v in p.spy_series],
                color="#7f7f7f", linewidth=1.5, linestyle="--", label=esc(f"SPY, same ${p.capital_cap:,.0f} held"))
    for i, (ts, sym, side, y) in enumerate(sorted(p.fills)):
        buy = side is Side.BUY
        tsl = ts.astimezone(ny)
        ax.scatter([tsl], [float(y)], marker="^" if buy else "v", color="#2ca02c" if buy else "#d62728", s=60, zorder=5)
        ax.annotate(f"{'▲' if buy else '▼'} {sym}", (tsl, float(y)), textcoords="offset points",
                    xytext=(6, 10 + 9 * (i % 6)), fontsize=7, color="#2ca02c" if buy else "#d62728")
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_title(esc(title), fontsize=12, loc="left")
    ax.set_ylabel(esc(f"P&L in $ (right axis: % of ${p.capital_cap:,.0f})"))
    ax.grid(True, alpha=0.25)
    locator = mdates.AutoDateLocator(minticks=4, maxticks=8, tz=ny)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, tz=ny))
    ax.set_xlabel("New York time")
    ax2 = ax.twinx()
    y0, y1 = ax.get_ylim()
    cap = float(p.capital_cap) or 1.0
    ax2.set_ylim(y0 / cap * 100, y1 / cap * 100)
    ax2.set_ylabel("%")
    ax.legend(loc="upper left", fontsize=8, frameon=False)
    sub = (f"since inception {_money(p.since_inception)} ({_pct(p, p.since_inception)})   "
           f"week {_money(p.week)}   month {_money(p.month)}   today {_money(p.today)}")
    fig.text(0.01, 0.01, esc(sub), fontsize=8, color="#444444")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def _money(v: Decimal) -> str:
    sign = "+" if v >= 0 else "−"
    return f"{sign}${abs(v):,.2f}"


def _pct(p: Performance, v: Decimal) -> str:
    x = p.pct(v)
    return f"{'+' if x >= 0 else '−'}{abs(x):.2f}%"
