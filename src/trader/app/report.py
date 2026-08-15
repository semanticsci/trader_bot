"""Use case: reports. Honest numbers, no spin.

  * ``daily``  — what happened today: proposals, taps, fills, P&L.
  * ``weekly`` — equity vs. a week ago, vs. the benchmark, vs. the stated target.

Reports are plain text so they read fine in Telegram, a terminal, or an email.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from trader.domain.models import ProposalStatus, utcnow
from trader.ports import BrokerPort, JournalPort, MarketDataPort


def daily_report(broker: BrokerPort, journal: JournalPort, mode: str) -> str:
    since = utcnow() - timedelta(hours=20)
    acct = broker.get_account()
    proposals = journal.list_proposals(since)
    orders = journal.list_broker_orders(since)

    lines = [f"📒 Daily summary [{mode.upper()}]"]
    lines.append(f"Equity ${acct.equity:,.2f}  ({acct.day_pl_pct:+.2%} today)  cash ${acct.cash:,.2f}")

    if proposals:
        for p in proposals:
            lines.append(
                f"Proposal {p.id}: {p.status.value} — {len(p.accepted)} passed gate, {len(p.rejected)} rejected"
            )
    else:
        lines.append("No proposal was made today.")

    if orders:
        lines.append("Orders:")
        for o in orders:
            fill = f"filled {o.filled_qty.normalize()} @ ${o.filled_avg_price}" if o.filled_avg_price else o.status
            lines.append(f" • {o.side.value.upper()} {o.qty.normalize()} {o.symbol} — {fill}")
    else:
        lines.append("No orders submitted.")

    if acct.positions:
        lines.append("Positions:")
        for pos in sorted(acct.positions, key=lambda p: -p.market_value):
            lines.append(
                f" • {pos.symbol}: {pos.qty.normalize()} @ ${pos.avg_entry_price} → ${pos.current_price} "
                f"({pos.unrealized_plpc:+.2%}, ${pos.unrealized_pl:+,.2f})"
            )
    return "\n".join(lines)


def weekly_report(
    broker: BrokerPort,
    data: MarketDataPort,
    journal: JournalPort,
    mode: str,
    benchmark: str,
    weekly_target_pct: Decimal,
) -> str:
    since = utcnow() - timedelta(days=7)
    acct = broker.get_account()
    proposals = journal.list_proposals(since)
    orders = journal.list_broker_orders(since)

    # Equity a week ago: the first proposal's snapshot this week is the most honest anchor we
    # have without a portfolio-history endpoint (kept broker-agnostic on purpose).
    start_equity: Decimal | None = None
    for p in proposals:
        eq = p.snapshot.get("account", {}).get("equity")
        if eq is not None:
            start_equity = Decimal(str(eq))
            break

    lines = [f"📈 Weekly report [{mode.upper()}]"]
    if start_equity and start_equity > 0:
        ret = (acct.equity - start_equity) / start_equity
        lines.append(f"Equity ${start_equity:,.2f} → ${acct.equity:,.2f}  ({ret:+.2%} this week)")
        met = "✅ met" if ret * 100 >= weekly_target_pct else "❌ not met"
        lines.append(f"Target was {weekly_target_pct:+.1f}%  →  {met}")
    else:
        lines.append(f"Equity ${acct.equity:,.2f} (no earlier snapshot this week to compare against)")

    bars = data.get_daily_bars([benchmark], 7).get(benchmark, [])
    if len(bars) >= 2 and bars[0].close > 0:
        b_ret = (bars[-1].close - bars[0].close) / bars[0].close
        lines.append(f"{benchmark} over the same window: {b_ret:+.2%}")

    counts: dict[str, int] = {}
    for p in proposals:
        counts[p.status.value] = counts.get(p.status.value, 0) + 1
    lines.append(
        "Proposals: "
        + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) if counts else "none")
    )
    approved = sum(1 for p in proposals if p.status is ProposalStatus.SUBMITTED)
    lines.append(f"Orders submitted: {len(orders)} across {approved} approved proposal(s)")

    lines.append("")
    lines.append(
        "Reminder: one week is noise. Judge the process (did the gate catch mistakes? were rationales "
        "sound?) before judging the return."
    )
    return "\n".join(lines)
