"""The vocabulary of the system.

Every concept the pipeline talks about is defined here as a small, immutable
dataclass. Money is always ``Decimal`` (never ``float`` — floats can't represent
0.1 exactly, and brokers reject prices like 181.19999999). Times are always
timezone-aware UTC.
"""

from __future__ import annotations

import enum
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

Money = Decimal


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


class Side(enum.StrEnum):
    """Which direction an order goes."""

    BUY = "buy"
    SELL = "sell"


class ProposalStatus(enum.StrEnum):
    """Lifecycle of a proposal, in order."""

    PENDING = "pending"  # sent to the human, waiting for a tap
    APPROVED = "approved"  # human tapped Go ahead; submission in progress
    SUBMITTED = "submitted"  # orders are at the broker
    SKIPPED = "skipped"  # human tapped Skip
    EXPIRED = "expired"  # nobody answered before expires_at
    EMPTY = "empty"  # the gate rejected everything / brain proposed nothing


# --------------------------------------------------------------------------- market state


@dataclass(frozen=True)
class Position:
    """A holding in the account."""

    symbol: str
    qty: Decimal
    avg_entry_price: Money
    current_price: Money
    market_value: Money
    unrealized_pl: Money
    unrealized_plpc: Decimal  # e.g. Decimal("0.0421") for +4.21%


@dataclass(frozen=True)
class Bar:
    """One day of price history."""

    date: str  # ISO date "2026-08-14" (a string so it JSON-serializes cleanly)
    open: Money
    high: Money
    low: Money
    close: Money
    volume: int


@dataclass(frozen=True)
class Indicators:
    """Simple, explainable technical indicators computed from bars.

    None of these are magic. They exist so the brain (and you) can see the
    trend without eyeballing 60 numbers. See ``indicators.py``.
    """

    last_close: Money
    sma_20: Money | None
    sma_50: Money | None
    high_20d: Money | None
    low_20d: Money | None
    return_5d_pct: Decimal | None
    return_20d_pct: Decimal | None
    avg_volume_20d: int | None


@dataclass(frozen=True)
class Quote:
    """The current price of a symbol and how it moved since yesterday."""

    symbol: str
    price: Money
    prev_close: Money | None
    change_pct: Decimal | None  # today vs prev close


@dataclass(frozen=True)
class Account:
    """The account as the broker reports it."""

    equity: Money
    cash: Money
    buying_power: Money
    last_equity: Money  # equity at yesterday's close — for the daily-loss breaker
    positions: tuple[Position, ...] = ()

    @property
    def day_pl_pct(self) -> Decimal:
        """Today's P&L as a fraction of yesterday's closing equity."""
        if self.last_equity == 0:
            return Decimal("0")
        return (self.equity - self.last_equity) / self.last_equity

    def position_for(self, symbol: str) -> Position | None:
        for p in self.positions:
            if p.symbol == symbol:
                return p
        return None


@dataclass(frozen=True)
class MarketSnapshot:
    """Everything the brain sees when it decides. Also everything the journal keeps."""

    taken_at: datetime
    market_open: bool
    account: Account
    quotes: dict[str, Quote]
    indicators: dict[str, Indicators]
    universe: tuple[str, ...]
    # The most capital the strategy may deploy (see RiskConfig.capital_cap). Shown to the brain so
    # it sizes positions against the *budget*, not the whole account. None = whole account.
    capital_cap: Money | None = None
    # Orders already at the broker but not filled. They are *committed* capital: the gate counts
    # pending buys toward the cap, and the brain should not re-propose them.
    open_orders: tuple[BrokerOrder, ...] = ()

    @property
    def pending_buy_notional(self) -> Money:
        """Dollar value of open BUY orders (qty x limit), i.e. cash already spoken for."""
        return sum(
            ((o.qty - o.filled_qty) * (o.limit_price or Decimal("0")) for o in self.open_orders if o.side is Side.BUY),
            Decimal("0"),
        )

    def pending_buy_notional_for(self, symbol: str) -> Money:
        return sum(
            ((o.qty - o.filled_qty) * (o.limit_price or Decimal("0"))
             for o in self.open_orders if o.side is Side.BUY and o.symbol == symbol),
            Decimal("0"),
        )

    def pending_sell_qty_for(self, symbol: str) -> Decimal:
        pending = (o for o in self.open_orders if o.side is Side.SELL and o.symbol == symbol)
        return sum(((o.qty - o.filled_qty) for o in pending), Decimal("0"))

    @property
    def capital_base(self) -> Money:
        """Equity the percentage rules are measured against: min(equity, capital_cap)."""
        eq = self.account.equity
        return min(eq, self.capital_cap) if self.capital_cap is not None else eq

    @property
    def invested(self) -> Money:
        """Market value of everything currently held."""
        return sum((p.market_value for p in self.account.positions), Decimal("0"))

    def to_json_dict(self) -> dict[str, Any]:
        """A JSON-safe dict (Decimals become strings, datetimes become ISO)."""
        return json.loads(json.dumps(asdict(self), default=_json_default))


# --------------------------------------------------------------------------- decisions


@dataclass(frozen=True)
class ProposedOrder:
    """One order the brain wants to place. Always a LIMIT order (never market)."""

    symbol: str
    side: Side
    qty: Decimal
    limit_price: Money
    rationale: str

    @property
    def notional(self) -> Money:
        return (self.qty * self.limit_price).quantize(Decimal("0.01"))

    def describe(self) -> str:
        arrow = "≤" if self.side is Side.BUY else "≥"
        return (
            f"{self.side.value.upper()} {self.qty.normalize()} {self.symbol} "
            f"@ {arrow} ${self.limit_price} (~${self.notional})"
        )


@dataclass(frozen=True)
class Decision:
    """What came back from the brain, before the gate looked at it."""

    orders: tuple[ProposedOrder, ...]
    summary: str  # one-paragraph market read, shown to the human
    raw: str = ""  # the raw model output, kept for the journal


@dataclass(frozen=True)
class GateResult:
    """The risk gate's verdict on one proposed order."""

    order: ProposedOrder
    accepted: bool
    reasons: tuple[str, ...] = ()  # why it was rejected (empty when accepted)


@dataclass
class Proposal:
    """A gated set of orders awaiting a human decision.

    This is the one mutable object in the domain, because its ``status`` moves
    through a lifecycle. Everything else about it is fixed at creation.
    """

    id: str
    created_at: datetime
    expires_at: datetime
    mode: str  # "paper" or "live"
    accepted: tuple[ProposedOrder, ...]
    rejected: tuple[GateResult, ...]
    summary: str
    status: ProposalStatus = ProposalStatus.PENDING
    telegram_message_id: int | None = None
    snapshot: dict[str, Any] = field(default_factory=dict)  # MarketSnapshot.to_json_dict()
    decision_raw: str = ""

    @staticmethod
    def new_id() -> str:
        # Short, unambiguous, fits in a Telegram callback payload (64 bytes max).
        return uuid.uuid4().hex[:12]

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or utcnow()) >= self.expires_at

    def is_actionable(self, now: datetime | None = None) -> bool:
        return self.status is ProposalStatus.PENDING and not self.is_expired(now)


@dataclass(frozen=True)
class BrokerOrder:
    """What the broker gives back after we submit."""

    broker_order_id: str
    symbol: str
    side: Side
    qty: Decimal
    limit_price: Money | None
    status: str  # broker's status string, e.g. "accepted", "filled"
    filled_qty: Decimal = Decimal("0")
    filled_avg_price: Money | None = None
    submitted_at: datetime | None = None
    filled_at: datetime | None = None


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class RiskConfig:
    """Hard limits enforced by the gate. Mirrors ``[risk]`` in config.toml."""

    # Hard ceiling on capital deployed (sum of position values + new buys). Percentage rules are
    # measured against min(equity, capital_cap), so a $100k paper account can honestly simulate a
    # $1,000 one. None = no cap, use full equity.
    capital_cap: Money | None = None
    max_position_pct: Decimal = Decimal("0.25")
    max_order_notional: Money = Decimal("400")
    max_orders_per_proposal: int = 3
    min_cash_buffer_pct: Decimal = Decimal("0.10")
    max_daily_loss_pct: Decimal = Decimal("0.03")
    max_limit_distance_pct: Decimal = Decimal("0.03")
    allow_fractional: bool = True
    allow_shorting: bool = False
    proposal_ttl: timedelta = timedelta(hours=7)
    blocked_symbols: frozenset[str] = frozenset()


# --------------------------------------------------------------------------- helpers


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, enum.Enum):
        return o.value
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def to_json(obj: Any) -> str:
    """Serialize any domain object (dataclass or dict) to a JSON string."""
    if hasattr(obj, "__dataclass_fields__"):
        obj = asdict(obj)
    return json.dumps(obj, default=_json_default, indent=2, sort_keys=True)
