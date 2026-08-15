"""The risk gate: code-enforced limits the brain cannot talk its way past.

This is the most important file in the project. The LLM *proposes*; this
module *disposes*. Every rule is a plain function that returns a reason string
when it fails, so a rejection is always explainable to the human ("rejected:
would make NVDA 31% of equity, cap is 25%").

Design rules for this file:
  * Pure: no I/O, no clock, no randomness. Everything it needs is passed in.
  * Conservative: when in doubt, reject. A missed trade costs nothing.
  * Explainable: every rejection has a human-readable reason.
"""

from __future__ import annotations

from decimal import Decimal

from trader.domain.models import (
    Account,
    CancelRequest,
    GateResult,
    MarketSnapshot,
    ProposedOrder,
    RiskConfig,
    Side,
)


def evaluate_cancels(
    cancels: list[CancelRequest] | tuple[CancelRequest, ...],
    snapshot: MarketSnapshot,
) -> tuple[list[CancelRequest], list[tuple[CancelRequest, str]]]:
    """Validate cancel requests against the broker's open orders.

    Returns (accepted, rejected-with-reason). Accepted cancels are enriched with the order's
    symbol/side/qty/limit so the human sees exactly what goes away.
    """
    open_by_id = {o.broker_order_id: o for o in snapshot.open_orders}
    accepted: list[CancelRequest] = []
    rejected: list[tuple[CancelRequest, str]] = []
    seen: set[str] = set()
    for c in cancels:
        o = open_by_id.get(c.broker_order_id)
        if o is None:
            rejected.append((c, "no open order with that id (already filled, cancelled, or a typo)"))
        elif c.broker_order_id in seen:
            rejected.append((c, "duplicate cancel"))
        elif not c.reason.strip():
            rejected.append((c, "no reason given"))
        else:
            seen.add(c.broker_order_id)
            accepted.append(
                CancelRequest(c.broker_order_id, c.reason, o.symbol, o.side, o.qty - o.filled_qty, o.limit_price)
            )
    return accepted, rejected


def evaluate(
    orders: list[ProposedOrder] | tuple[ProposedOrder, ...],
    snapshot: MarketSnapshot,
    config: RiskConfig,
    *,
    halted: bool = False,
    cancels: tuple[CancelRequest, ...] | list[CancelRequest] = (),
) -> list[GateResult]:
    """Run every proposed order through every rule.

    Orders are evaluated in the order given, and *cumulatively*: if the first
    two buys use up the cash buffer, the third is rejected even though it
    would pass on its own. That mirrors what happens at the broker.

    Args:
        orders: what the brain proposed.
        snapshot: the market/account state the brain saw.
        config: hard limits from config.toml.
        halted: True if the kill-switch file exists — rejects everything.
        cancels: already-validated cancels in the same proposal; the capital they free up is
            available to the buys evaluated here.
    """
    account = snapshot.account
    results: list[GateResult] = []
    accepted_count = 0
    # Buys already sitting at the broker (unfilled) are committed capital — count them from the
    # start, minus anything this proposal cancels.
    cancelled_ids = {c.broker_order_id for c in cancels}
    cash_committed = sum(
        ((o.qty - o.filled_qty) * (o.limit_price or Decimal("0"))
         for o in snapshot.open_orders if o.side is Side.BUY and o.broker_order_id not in cancelled_ids),
        Decimal("0"),
    )
    # Percentage rules are measured against the capital *budget*, not necessarily the whole account
    # (a $100k paper account simulating $1,000 — see RiskConfig.capital_cap).
    capital_cap = config.capital_cap if config.capital_cap is not None else snapshot.capital_cap
    base = min(account.equity, capital_cap) if capital_cap is not None else account.equity
    invested = snapshot.invested  # market value already deployed (positions), before this proposal
    # Position values after the buys/sells accepted so far (symbol -> market value)
    projected_value: dict[str, Decimal] = {p.symbol: p.market_value for p in account.positions}
    projected_qty: dict[str, Decimal] = {p.symbol: p.qty for p in account.positions}
    for o in snapshot.open_orders:  # pending buys add exposure; pending sells reduce what's sellable
        if o.broker_order_id in cancelled_ids:
            continue
        remaining = o.qty - o.filled_qty
        if o.side is Side.BUY:
            pending_value = remaining * (o.limit_price or Decimal("0"))
            projected_value[o.symbol] = projected_value.get(o.symbol, Decimal("0")) + pending_value
        else:
            projected_qty[o.symbol] = projected_qty.get(o.symbol, Decimal("0")) - remaining

    daily_breaker_tripped = account.day_pl_pct <= -config.max_daily_loss_pct

    for order in orders:
        reasons: list[str] = []

        if halted:
            reasons.append("kill switch is on (HALT file present) — all orders blocked")

        reasons += _check_shape(order, config)
        reasons += _check_symbol(order, snapshot, config)
        reasons += _check_limit_distance(order, snapshot, config)

        if accepted_count >= config.max_orders_per_proposal:
            reasons.append(f"more than {config.max_orders_per_proposal} orders in one proposal")

        if order.side is Side.BUY:
            if daily_breaker_tripped:
                reasons.append(
                    f"daily-loss breaker tripped: equity is {account.day_pl_pct:.2%} vs "
                    f"yesterday, limit is -{config.max_daily_loss_pct:.0%} — no new buys today"
                )
            reasons += _check_buy_size(
                order, account, base, config, cash_committed, projected_value.get(order.symbol, Decimal("0"))
            )
            reasons += _check_capital_cap(order, invested, cash_committed, capital_cap)
        else:
            reasons += _check_sell(order, projected_qty.get(order.symbol, Decimal("0")), config)

        ok = not reasons
        results.append(GateResult(order=order, accepted=ok, reasons=tuple(reasons)))

        if ok:
            accepted_count += 1
            if order.side is Side.BUY:
                cash_committed += order.notional
                projected_value[order.symbol] = (
                    projected_value.get(order.symbol, Decimal("0")) + order.notional
                )
                projected_qty[order.symbol] = projected_qty.get(order.symbol, Decimal("0")) + order.qty
            else:
                projected_qty[order.symbol] = projected_qty.get(order.symbol, Decimal("0")) - order.qty
                projected_value[order.symbol] = max(
                    Decimal("0"), projected_value.get(order.symbol, Decimal("0")) - order.notional
                )

    return results


# --------------------------------------------------------------------------- individual rules
# Each returns a list of reasons; empty list means "this rule is happy".


def _check_shape(order: ProposedOrder, config: RiskConfig) -> list[str]:
    reasons = []
    if order.qty <= 0:
        reasons.append("quantity must be positive")
    if order.limit_price <= 0:
        reasons.append("limit price must be positive")
    if not config.allow_fractional and order.qty != order.qty.to_integral_value():
        reasons.append("fractional shares are disabled (allow_fractional = false)")
    if not order.rationale.strip():
        reasons.append("no rationale given — the brain must explain every order")
    return reasons


def _check_symbol(order: ProposedOrder, snapshot: MarketSnapshot, config: RiskConfig) -> list[str]:
    reasons = []
    sym = order.symbol.upper()
    if sym in config.blocked_symbols:
        reasons.append(f"{sym} is on the blocked list")
    held = snapshot.account.position_for(sym) is not None
    if sym not in snapshot.universe and not held:
        reasons.append(f"{sym} is not in the configured universe (and not currently held)")
    if sym not in snapshot.quotes:
        reasons.append(f"no current quote for {sym} — refusing to trade blind")
    return reasons


def _check_limit_distance(
    order: ProposedOrder, snapshot: MarketSnapshot, config: RiskConfig
) -> list[str]:
    quote = snapshot.quotes.get(order.symbol.upper())
    if quote is None or quote.price <= 0:
        return []  # already rejected by _check_symbol
    distance = abs(order.limit_price - quote.price) / quote.price
    if distance > config.max_limit_distance_pct:
        return [
            f"limit ${order.limit_price} is {distance:.1%} from current ${quote.price} "
            f"(max {config.max_limit_distance_pct:.0%}) — looks like a mistake"
        ]
    return []


def _check_buy_size(
    order: ProposedOrder,
    account: Account,
    base: Decimal,
    config: RiskConfig,
    cash_already_committed: Decimal,
    current_symbol_value: Decimal,
) -> list[str]:
    """Size rules. ``base`` is min(equity, capital_cap) — the capital we're actually managing."""
    reasons = []
    notional = order.notional

    if notional > config.max_order_notional:
        reasons.append(f"order is ${notional}, max single order is ${config.max_order_notional}")

    if base > 0:
        after = current_symbol_value + notional
        pct = after / base
        if pct > config.max_position_pct:
            reasons.append(
                f"would make {order.symbol} {pct:.0%} of capital, cap is {config.max_position_pct:.0%}"
            )

    buffer = base * config.min_cash_buffer_pct
    cash_after = account.cash - cash_already_committed - notional
    if cash_after < buffer:
        reasons.append(
            f"would leave ${cash_after:.2f} cash, below the {config.min_cash_buffer_pct:.0%} "
            f"buffer (${buffer:.2f})"
        )
    return reasons


def _check_capital_cap(
    order: ProposedOrder, invested_now: Decimal, buys_committed: Decimal, cap: Decimal | None
) -> list[str]:
    """Total deployed capital (held + buys in this proposal) may not exceed the cap."""
    if cap is None:
        return []
    after = invested_now + buys_committed + order.notional
    if after > cap:
        return [f"would put ${after:.2f} to work, capital cap is ${cap:.2f}"]
    return []


def _check_sell(order: ProposedOrder, held_qty: Decimal, config: RiskConfig) -> list[str]:
    if held_qty <= 0 and not config.allow_shorting:
        return [f"you don't hold {order.symbol} and shorting is disabled"]
    if order.qty > held_qty and not config.allow_shorting:
        return [
            f"selling {order.qty.normalize()} but only hold {held_qty.normalize()} "
            f"{order.symbol} — would open a short"
        ]
    return []


def summarize(results: list[GateResult]) -> str:
    """One line for logs and Telegram: '2 of 3 passed; 1 rejected'."""
    passed = sum(1 for r in results if r.accepted)
    return f"{passed} of {len(results)} passed the risk gate"
