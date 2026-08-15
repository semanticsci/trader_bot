"""Alpaca adapter: implements ``BrokerPort`` and ``MarketDataPort``.

Alpaca has two separate APIs (trading vs. market data) so this file wraps
both. Everything Alpaca returns is converted into our own domain models at the
boundary — the rest of the code never sees an Alpaca object.

Paper vs live is a single flag on the client. Same code, different account.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest

from trader.domain.indicators import pct_change
from trader.domain.models import Account, Bar, BrokerOrder, Position, ProposedOrder, Quote, Side

log = logging.getLogger(__name__)


def _dec(value: object, default: str = "0") -> Decimal:
    """Alpaca returns numbers as strings or floats; normalize to Decimal via str()."""
    if value is None:
        return Decimal(default)
    return Decimal(str(value))


class AlpacaBroker:
    """Trading + market data through Alpaca."""

    def __init__(self, api_key: str, secret_key: str, *, paper: bool = True) -> None:
        self.paper = paper
        self._trading = TradingClient(api_key, secret_key, paper=paper)
        # The free data plan gets IEX (one exchange's view of prices). Good enough for daily
        # decisions; not for high-frequency anything.
        self._data = StockHistoricalDataClient(api_key, secret_key)
        self._feed = DataFeed.IEX

    # ------------------------------------------------------------------ BrokerPort

    def get_account(self) -> Account:
        acct = self._trading.get_account()
        positions = tuple(
            Position(
                symbol=str(p.symbol),
                qty=_dec(p.qty),
                avg_entry_price=_dec(p.avg_entry_price),
                current_price=_dec(p.current_price),
                market_value=_dec(p.market_value),
                unrealized_pl=_dec(p.unrealized_pl),
                unrealized_plpc=_dec(p.unrealized_plpc),
            )
            for p in self._trading.get_all_positions()
        )
        return Account(
            equity=_dec(acct.equity),
            cash=_dec(acct.cash),
            buying_power=_dec(acct.buying_power),
            last_equity=_dec(acct.last_equity, default=str(acct.equity)),
            positions=positions,
        )

    def is_market_open(self) -> bool:
        return bool(self._trading.get_clock().is_open)

    def submit_limit_order(self, order: ProposedOrder, client_order_id: str) -> BrokerOrder:
        req = LimitOrderRequest(
            symbol=order.symbol,
            qty=float(order.qty),
            side=OrderSide.BUY if order.side is Side.BUY else OrderSide.SELL,
            type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=float(order.limit_price),
            client_order_id=client_order_id,
        )
        log.info("submitting %s (client_order_id=%s, paper=%s)", order.describe(), client_order_id, self.paper)
        raw = self._trading.submit_order(req)
        return self._to_broker_order(raw)

    def get_orders_since(self, since: datetime) -> list[BrokerOrder]:
        req = GetOrdersRequest(status=QueryOrderStatus.ALL, after=since, limit=200)
        return [self._to_broker_order(o) for o in self._trading.get_orders(req)]

    # ------------------------------------------------------------------ MarketDataPort

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        if not symbols:
            return {}
        snaps = self._data.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=symbols, feed=self._feed)
        )
        quotes: dict[str, Quote] = {}
        for sym, snap in snaps.items():
            price = None
            if snap.latest_trade is not None:
                price = _dec(snap.latest_trade.price)
            elif snap.daily_bar is not None:
                price = _dec(snap.daily_bar.close)
            if price is None or price <= 0:
                continue
            prev_close = _dec(snap.previous_daily_bar.close) if snap.previous_daily_bar else None
            quotes[sym] = Quote(
                symbol=sym,
                price=price,
                prev_close=prev_close,
                change_pct=pct_change(price, prev_close) if prev_close else None,
            )
        return quotes

    def get_daily_bars(self, symbols: list[str], days: int) -> dict[str, list[Bar]]:
        if not symbols:
            return {}
        # Ask for a bit more calendar time than trading days, since weekends/holidays are gaps.
        start = datetime.now(UTC) - timedelta(days=int(days * 1.6) + 5)
        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=start,
            feed=self._feed,
        )
        barset = self._data.get_stock_bars(req)
        out: dict[str, list[Bar]] = {}
        for sym in symbols:
            raw_bars = barset.data.get(sym, []) if hasattr(barset, "data") else barset[sym]
            bars = [
                Bar(
                    date=b.timestamp.date().isoformat(),
                    open=_dec(b.open),
                    high=_dec(b.high),
                    low=_dec(b.low),
                    close=_dec(b.close),
                    volume=int(b.volume),
                )
                for b in raw_bars
            ]
            out[sym] = bars[-days:]
        return out

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _to_broker_order(o: object) -> BrokerOrder:
        side_val = str(getattr(o, "side", "buy")).lower()
        status = getattr(o, "status", "unknown")
        status_str = status.value if hasattr(status, "value") else str(status)
        return BrokerOrder(
            broker_order_id=str(getattr(o, "id", "")),
            symbol=str(getattr(o, "symbol", "")),
            side=Side.SELL if "sell" in side_val else Side.BUY,
            qty=_dec(getattr(o, "qty", None)),
            limit_price=_dec(getattr(o, "limit_price", None)) if getattr(o, "limit_price", None) else None,
            status=status_str,
            filled_qty=_dec(getattr(o, "filled_qty", None)),
            filled_avg_price=(
                _dec(getattr(o, "filled_avg_price", None))
                if getattr(o, "filled_avg_price", None)
                else None
            ),
            submitted_at=getattr(o, "submitted_at", None),
            filled_at=getattr(o, "filled_at", None),
        )
