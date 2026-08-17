"""An in-memory broker + market data for tests and ``--dry-run``.

Nothing here touches the network. It behaves like a very polite broker that
accepts every limit order and fills it instantly at the limit price, which is
enough to exercise the whole pipeline end to end.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from trader.domain.models import (
    Account,
    Bar,
    BrokerOrder,
    Position,
    ProposedOrder,
    Quote,
    Side,
    utcnow,
)


class FakeBroker:
    """Implements BrokerPort and MarketDataPort without any I/O."""

    def __init__(
        self,
        *,
        equity: Decimal = Decimal("1000"),
        cash: Decimal = Decimal("1000"),
        last_equity: Decimal | None = None,
        positions: tuple[Position, ...] = (),
        prices: dict[str, Decimal] | None = None,
        market_open: bool = True,
    ) -> None:
        self.account = Account(
            equity=equity,
            cash=cash,
            buying_power=cash,
            last_equity=last_equity if last_equity is not None else equity,
            positions=positions,
        )
        self.prices: dict[str, Decimal] = prices or {}
        self.market_open = market_open
        self.submitted: list[BrokerOrder] = []
        self.client_order_ids: set[str] = set()
        self.open_orders: list[BrokerOrder] = []  # tests can pre-load pending orders here
        self.cancelled: list[str] = []
        self.equity_history: list[tuple[datetime, Decimal]] = []

    # BrokerPort
    def get_account(self) -> Account:
        return self.account

    def is_market_open(self) -> bool:
        return self.market_open

    def submit_limit_order(self, order: ProposedOrder, client_order_id: str) -> BrokerOrder:
        if client_order_id in self.client_order_ids:
            # Idempotency: the same client id never creates a second order.
            return next(o for o in self.submitted if o.broker_order_id == f"fake-{client_order_id}")
        self.client_order_ids.add(client_order_id)
        bo = BrokerOrder(
            broker_order_id=f"fake-{client_order_id}",
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            limit_price=order.limit_price,
            status="filled",
            filled_qty=order.qty,
            filled_avg_price=order.limit_price,
            submitted_at=utcnow(),
            filled_at=utcnow(),
        )
        self.submitted.append(bo)
        return bo

    def get_orders_since(self, since: datetime) -> list[BrokerOrder]:
        return [o for o in self.submitted if o.submitted_at and o.submitted_at >= since]

    def get_open_orders(self) -> list[BrokerOrder]:
        return list(self.open_orders)

    def get_equity_history(self, days: int) -> list[tuple[datetime, Decimal]]:
        return list(self.equity_history)

    def cancel_order(self, broker_order_id: str) -> None:
        before = len(self.open_orders)
        self.open_orders = [o for o in self.open_orders if o.broker_order_id != broker_order_id]
        if len(self.open_orders) == before:
            raise ValueError(f"order {broker_order_id} is not open")
        self.cancelled.append(broker_order_id)

    # MarketDataPort
    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {
            s: Quote(symbol=s, price=p, prev_close=p, change_pct=Decimal("0"))
            for s, p in self.prices.items()
            if s in symbols
        }

    def get_news(self, symbols: list[str], limit: int = 30) -> dict[str, list[str]]:
        return {}

    def get_daily_bars(self, symbols: list[str], days: int) -> dict[str, list[Bar]]:
        out: dict[str, list[Bar]] = {}
        for s in symbols:
            p = self.prices.get(s)
            if p is None:
                continue
            out[s] = [
                Bar(date=f"2026-01-{(i % 28) + 1:02d}", open=p, high=p, low=p, close=p, volume=1000)
                for i in range(days)
            ]
        return out


def position(symbol: str, qty: str, price: str) -> Position:
    """Convenience for tests: build a Position at a flat price."""
    q, p = Decimal(qty), Decimal(price)
    return Position(
        symbol=symbol,
        qty=q,
        avg_entry_price=p,
        current_price=p,
        market_value=q * p,
        unrealized_pl=Decimal("0"),
        unrealized_plpc=Decimal("0"),
    )


def order(symbol: str, side: Side, qty: str, price: str, why: str = "test") -> ProposedOrder:
    """Convenience for tests."""
    return ProposedOrder(symbol=symbol, side=side, qty=Decimal(qty), limit_price=Decimal(price), rationale=why)
