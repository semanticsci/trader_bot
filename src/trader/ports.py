"""Ports: the interfaces the application needs from the outside world.

A *port* is just a ``Protocol`` — a description of the methods something must
have. The app layer only talks to ports. The adapters (Alpaca, Claude, Telegram,
SQLite) implement them. This is what makes it possible to:

  * unit-test the whole pipeline with a ``FakeBroker`` and no network,
  * swap Alpaca for Robinhood by writing one new file,
  * swap Claude-via-API for "a Cowork agent wrote decision.json".
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from trader.domain.models import (
    Account,
    Bar,
    BrokerOrder,
    Decision,
    MarketSnapshot,
    Proposal,
    ProposedOrder,
    Quote,
)


class BrokerPort(Protocol):
    """Reading and writing to a brokerage account."""

    def get_account(self) -> Account: ...

    def is_market_open(self) -> bool: ...

    def submit_limit_order(self, order: ProposedOrder, client_order_id: str) -> BrokerOrder:
        """Place a DAY limit order. ``client_order_id`` makes retries idempotent."""
        ...

    def get_orders_since(self, since: datetime) -> list[BrokerOrder]:
        """All orders (any status) submitted after ``since``."""
        ...

    def get_open_orders(self) -> list[BrokerOrder]:
        """Orders accepted by the broker but not yet filled/cancelled (pending exposure)."""
        ...

    def cancel_order(self, broker_order_id: str) -> None:
        """Cancel an open order. Raises if the broker refuses (e.g. already filled)."""
        ...

    def get_equity_history(self, days: int) -> list[tuple[datetime, Decimal]]:
        """(timestamp, account equity) points, oldest first, one per trading day (or finer)."""
        ...


class MarketDataPort(Protocol):
    """Prices and history."""

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]: ...

    def get_daily_bars(self, symbols: list[str], days: int) -> dict[str, list[Bar]]:
        """Chronological daily bars per symbol (oldest first)."""
        ...

    def get_news(self, symbols: list[str], limit: int = 30) -> dict[str, list[str]]:
        """Recent headlines per symbol (newest first). Empty dict if unsupported/unavailable."""
        ...


class DeciderPort(Protocol):
    """The brain. Given what the market looks like, what should we do?"""

    def decide(self, snapshot: MarketSnapshot, strategy_prompt: str) -> Decision: ...


class NotifierPort(Protocol):
    """Talking to the human."""

    def send_proposal(self, proposal: Proposal) -> int | None:
        """Send the proposal with Go ahead / Skip buttons. Returns the message id."""
        ...

    def send_text(self, text: str) -> None: ...

    def update_proposal_message(self, proposal: Proposal, footer: str) -> None:
        """Edit the original proposal message (e.g. to remove buttons and add a status line)."""
        ...


class JournalPort(Protocol):
    """Append-only memory of everything that happened. SQLite in practice."""

    def save_proposal(self, proposal: Proposal) -> None: ...

    def get_proposal(self, proposal_id: str) -> Proposal | None: ...

    def latest_pending(self) -> Proposal | None: ...

    def list_proposals(self, since: datetime) -> list[Proposal]: ...

    def record_broker_order(self, proposal_id: str, order: BrokerOrder) -> None: ...

    def list_broker_orders(self, since: datetime) -> list[BrokerOrder]: ...

    def log_event(self, kind: str, payload: dict[str, object]) -> None: ...

    def get_state(self, key: str) -> str | None: ...

    def set_state(self, key: str, value: str) -> None: ...
