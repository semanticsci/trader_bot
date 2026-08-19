"""End-to-end through the pipeline with fakes: propose -> journal -> tap -> submit."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from trader.adapters.fake_broker import FakeBroker, position
from trader.adapters.sqlite_journal import SqliteJournal
from trader.adapters.telegram_notifier import Tap
from trader.app.approve import ApproveDeps, handle_tap
from trader.app.propose import ProposeDeps, run_propose
from trader.domain.models import (
    Decision,
    MarketSnapshot,
    Proposal,
    ProposalStatus,
    ProposedOrder,
    Side,
    utcnow,
)

CHAT = "12345"


class ScriptedDecider:
    """A brain that always says the same thing — perfect for tests."""

    def __init__(self, orders: list[ProposedOrder], summary: str = "Test day.") -> None:
        self._d = Decision(orders=tuple(orders), summary=summary, raw="{}")

    def decide(self, snapshot: MarketSnapshot, strategy_prompt: str) -> Decision:
        return self._d


class FakeNotifier:
    """Records what would have gone to Telegram."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edits: list[tuple[str, str]] = []
        self.acks: list[str] = []
        self._next_id = 100

    def send_proposal(self, proposal: Proposal) -> int:
        self.sent.append(f"proposal:{proposal.id}")
        self._next_id += 1
        return self._next_id

    def send_text(self, text: str) -> None:
        self.sent.append(text)

    def update_proposal_message(self, proposal: Proposal, footer: str) -> None:
        self.edits.append((proposal.id, footer))

    def answer_callback(self, cq_id: str, text: str) -> None:
        self.acks.append(text)

    def poll(self, offset, timeout_s=20):
        return []


def _deps(broker: FakeBroker, journal: SqliteJournal, notifier: FakeNotifier, risk_config, orders):
    propose = ProposeDeps(
        broker=broker, data=broker, decider=ScriptedDecider(orders), journal=journal, notifier=notifier,
        risk_config=risk_config, universe=("AAPL", "NVDA", "SPY", "TQQQ"), history_days=30,
        mode="paper", strategy_prompt="buy dips", halted=False,
    )
    approve = ApproveDeps(
        broker=broker, data=broker, journal=journal, notifier=notifier, risk_config=risk_config,
        universe=("AAPL", "NVDA", "SPY", "TQQQ"), history_days=30, allowed_chat_id=CHAT,
    )
    return propose, approve


def test_full_cycle_approve_submits_only_gated_orders(broker, risk_config):
    # Arrange: brain proposes one good buy and one blocked symbol.
    journal = SqliteJournal(":memory:")
    notifier = FakeNotifier()
    good = ProposedOrder("NVDA", Side.BUY, Decimal("1"), Decimal("180"), "trend up")
    bad = ProposedOrder("TQQQ", Side.BUY, Decimal("1"), Decimal("50"), "yolo")
    propose_deps, approve_deps = _deps(broker, journal, notifier, risk_config, [good, bad])

    # Act 1: morning cycle
    proposal = run_propose(propose_deps)

    # Assert 1: journaled, pending, one accepted, one rejected, telegram message id stored
    assert proposal.status is ProposalStatus.PENDING
    assert [o.symbol for o in proposal.accepted] == ["NVDA"]
    assert [r.order.symbol for r in proposal.rejected] == ["TQQQ"]
    assert proposal.telegram_message_id == 101
    stored = journal.get_proposal(proposal.id)
    assert stored is not None and stored.status is ProposalStatus.PENDING
    assert stored.snapshot["account"]["equity"] == "1000"

    # Act 2: the human taps Go ahead
    tap = Tap(update_id=1, chat_id=CHAT, kind="approve", proposal_id=proposal.id, text="", callback_query_id="cq1")
    outcome = handle_tap(tap, approve_deps)

    # Assert 2: exactly the gated order was submitted, journaled, status updated, message edited
    assert outcome.action == "submitted" and outcome.submitted == 1
    assert [o.symbol for o in broker.submitted] == ["NVDA"]
    assert journal.get_proposal(proposal.id).status is ProposalStatus.SUBMITTED
    assert len(journal.list_broker_orders(utcnow() - timedelta(minutes=1))) == 1
    assert notifier.edits[-1][1].startswith("✅ Approved")


def test_skip_submits_nothing(broker, risk_config):
    journal = SqliteJournal(":memory:")
    notifier = FakeNotifier()
    good = ProposedOrder("NVDA", Side.BUY, Decimal("1"), Decimal("180"), "trend up")
    propose_deps, approve_deps = _deps(broker, journal, notifier, risk_config, [good])
    proposal = run_propose(propose_deps)

    outcome = handle_tap(Tap(2, CHAT, "skip", proposal.id, "", "cq2"), approve_deps)

    assert outcome.action == "skipped"
    assert broker.submitted == []
    assert journal.get_proposal(proposal.id).status is ProposalStatus.SKIPPED


def test_text_go_approves_latest_pending(broker, risk_config):
    journal = SqliteJournal(":memory:")
    notifier = FakeNotifier()
    good = ProposedOrder("NVDA", Side.BUY, Decimal("1"), Decimal("180"), "trend up")
    propose_deps, approve_deps = _deps(broker, journal, notifier, risk_config, [good])
    run_propose(propose_deps)

    outcome = handle_tap(Tap(3, CHAT, "text", None, "go ahead"), approve_deps)

    assert outcome.action == "submitted"
    assert len(broker.submitted) == 1


def test_unauthorized_chat_is_ignored(broker, risk_config):
    journal = SqliteJournal(":memory:")
    notifier = FakeNotifier()
    good = ProposedOrder("NVDA", Side.BUY, Decimal("1"), Decimal("180"), "trend up")
    propose_deps, approve_deps = _deps(broker, journal, notifier, risk_config, [good])
    proposal = run_propose(propose_deps)

    outcome = handle_tap(Tap(4, "999", "approve", proposal.id, "", "cq4"), approve_deps)

    assert outcome.action == "unauthorized"
    assert broker.submitted == []
    assert journal.get_proposal(proposal.id).status is ProposalStatus.PENDING


def test_expired_proposal_cannot_be_approved(broker, risk_config):
    """A zero-TTL config makes the proposal expire the instant it is created."""
    from dataclasses import replace

    journal = SqliteJournal(":memory:")
    notifier = FakeNotifier()
    good = ProposedOrder("NVDA", Side.BUY, Decimal("1"), Decimal("180"), "trend up")
    zero_ttl = replace(risk_config, proposal_ttl=timedelta(seconds=0))
    propose_deps, approve_deps = _deps(broker, journal, notifier, zero_ttl, [good])
    proposal = run_propose(propose_deps)

    outcome = handle_tap(Tap(5, CHAT, "approve", proposal.id, "", "cq5"), approve_deps)

    assert outcome.action == "expired"
    assert broker.submitted == []
    assert journal.get_proposal(proposal.id).status is ProposalStatus.EXPIRED


def test_approve_regates_on_fresh_data(broker, risk_config):
    """Prices moved between 8:30 and the tap → the stale limit is caught at submit time."""
    journal = SqliteJournal(":memory:")
    notifier = FakeNotifier()
    good = ProposedOrder("NVDA", Side.BUY, Decimal("1"), Decimal("180"), "trend up")
    propose_deps, approve_deps = _deps(broker, journal, notifier, risk_config, [good])
    proposal = run_propose(propose_deps)

    broker.prices["NVDA"] = Decimal("200")  # +11% since the proposal
    outcome = handle_tap(Tap(6, CHAT, "approve", proposal.id, "", "cq6"), approve_deps)

    assert outcome.submitted == 0 and outcome.gate_rejected == 1
    assert broker.submitted == []
    assert journal.get_proposal(proposal.id).status is ProposalStatus.SKIPPED


def test_halt_file_blocks_submission(broker, risk_config):
    journal = SqliteJournal(":memory:")
    notifier = FakeNotifier()
    good = ProposedOrder("NVDA", Side.BUY, Decimal("1"), Decimal("180"), "trend up")
    propose_deps, approve_deps = _deps(broker, journal, notifier, risk_config, [good])
    proposal = run_propose(propose_deps)
    approve_deps.halted_check = lambda: True

    outcome = handle_tap(Tap(7, CHAT, "approve", proposal.id, "", "cq7"), approve_deps)

    assert outcome.action == "skipped" and broker.submitted == []
    assert notifier.edits[-1][1].startswith("🛑")


def test_empty_decision_yields_empty_proposal(broker, risk_config):
    journal = SqliteJournal(":memory:")
    notifier = FakeNotifier()
    propose_deps, _ = _deps(broker, journal, notifier, risk_config, [])
    proposal = run_propose(propose_deps)
    assert proposal.status is ProposalStatus.EMPTY
    assert journal.latest_pending() is None


def test_idempotent_resubmit_does_not_duplicate(broker):
    """If the approver crashed after submitting and retried, the broker must not double-fill."""
    o = ProposedOrder("NVDA", Side.BUY, Decimal("1"), Decimal("180"), "x")
    a = broker.submit_limit_order(o, client_order_id="p1-NVDA-buy")
    b = broker.submit_limit_order(o, client_order_id="p1-NVDA-buy")
    assert a.broker_order_id == b.broker_order_id
    assert len(broker.submitted) == 1


def test_position_helper():
    p = position("AAPL", "2", "200")
    assert p.market_value == Decimal("400")


# ---------------------------------------------------------------- cancels: rotate out of an unfilled order


def _open(symbol, side, qty, price, oid):
    from trader.domain.models import BrokerOrder
    return BrokerOrder(oid, symbol, side, Decimal(qty), Decimal(price), "accepted")


def test_cancel_frees_capital_and_is_executed_on_tap(risk_config):
    """$1,000 cap, $949 pending. Buying $200 AMD only passes if the $239 SPY orders are cancelled first."""
    from dataclasses import replace

    from trader.domain.models import CancelRequest

    cfg = replace(risk_config, capital_cap=Decimal("1000"), min_cash_buffer_pct=Decimal("0"))
    b = FakeBroker(
        equity=Decimal("100000"), cash=Decimal("100000"), last_equity=Decimal("100000"),
        prices={"SPY": Decimal("776"), "AMD": Decimal("514"), "NVDA": Decimal("225"), "MSFT": Decimal("495"),
                "QQQ": Decimal("731"), "AMZN": Decimal("263")},
    )
    b.open_orders = [
        _open("NVDA", Side.BUY, "0.89", "224.03", "o-nvda"), _open("MSFT", Side.BUY, "0.4", "492.87", "o-msft"),
        _open("SPY", Side.BUY, "0.25", "772.15", "o-spy1"), _open("SPY", Side.BUY, "0.06", "772.15", "o-spy2"),
        _open("QQQ", Side.BUY, "0.34", "727.35", "o-qqq"), _open("AMZN", Side.BUY, "0.43", "261.33", "o-amzn"),
    ]
    journal = SqliteJournal(":memory:")
    notifier = FakeNotifier()
    amd = ProposedOrder("AMD", Side.BUY, Decimal("0.39"), Decimal("511.83"), "strongest 5d momentum")

    class RotatingDecider:
        def decide(self, snapshot, strategy):
            return Decision(orders=(amd,), summary="rotate SPY -> AMD", raw="{}",
                            cancels=(CancelRequest("o-spy1", "dead weight"), CancelRequest("o-spy2", "dead weight"),
                                     CancelRequest("o-nope", "typo")))

    propose = ProposeDeps(broker=b, data=b, decider=RotatingDecider(), journal=journal, notifier=notifier,
                          risk_config=cfg, universe=("SPY", "AMD", "NVDA", "MSFT", "QQQ", "AMZN"), history_days=30,
                          mode="paper", strategy_prompt="", halted=False)
    approve = ApproveDeps(broker=b, data=b, journal=journal, notifier=notifier, risk_config=cfg,
                          universe=("SPY", "AMD", "NVDA", "MSFT", "QQQ", "AMZN"), history_days=30, allowed_chat_id=CHAT)

    proposal = run_propose(propose)

    # gate: both real cancels accepted (enriched with symbol/qty), the typo rejected, and the AMD buy passes
    assert [c.symbol for c in proposal.cancels] == ["SPY", "SPY"] and proposal.cancels[0].qty == Decimal("0.25")
    assert len(proposal.rejected_cancels) == 1 and "no open order" in proposal.rejected_cancels[0][1]
    assert [o.symbol for o in proposal.accepted] == ["AMD"], proposal.rejected
    stored = journal.get_proposal(proposal.id)
    assert [c.broker_order_id for c in stored.cancels] == ["o-spy1", "o-spy2"]

    # tap: cancels executed at the broker, then AMD submitted
    outcome = handle_tap(Tap(9, CHAT, "approve", proposal.id, "", "cq9"), approve)
    assert outcome.action == "submitted" and outcome.submitted == 1
    assert b.cancelled == ["o-spy1", "o-spy2"]
    assert [o.symbol for o in b.submitted] == ["AMD"]
    assert not any(o.symbol == "SPY" for o in b.open_orders)


def test_buy_without_cancel_is_rejected_when_cap_is_full(risk_config):
    """Same book, no cancel → the AMD buy breaks the cap and the gate says so."""
    from dataclasses import replace

    from .conftest import make_snapshot

    cfg = replace(risk_config, capital_cap=Decimal("1000"), min_cash_buffer_pct=Decimal("0"))
    b = FakeBroker(equity=Decimal("100000"), cash=Decimal("100000"), prices={"SPY": Decimal("776"), "AMD": Decimal("514")})
    b.open_orders = [_open("SPY", Side.BUY, "1.2", "772.15", "o-spy")]  # ~$927 pending
    snap = replace(make_snapshot(b, universe=("SPY", "AMD")), open_orders=tuple(b.open_orders))
    from trader.domain import risk
    res = risk.evaluate([ProposedOrder("AMD", Side.BUY, Decimal("0.39"), Decimal("511.83"), "x")], snap, cfg)
    assert not res[0].accepted and "capital cap" in " ".join(res[0].reasons)


def test_stale_cancel_at_tap_time_is_reported_not_fatal(risk_config):
    """If the order filled between proposal and tap, the cancel is skipped with a reason; the rest proceeds."""
    from dataclasses import replace

    from trader.domain.models import CancelRequest

    cfg = replace(risk_config, capital_cap=Decimal("1000"), min_cash_buffer_pct=Decimal("0"))
    b = FakeBroker(equity=Decimal("100000"), cash=Decimal("100000"), prices={"SPY": Decimal("776"), "AMD": Decimal("514")})
    b.open_orders = [_open("SPY", Side.BUY, "0.25", "772.15", "o-spy")]
    journal = SqliteJournal(":memory:")
    notifier = FakeNotifier()

    class D:
        def decide(self, s, st):
            return Decision(orders=(ProposedOrder("AMD", Side.BUY, Decimal("0.2"), Decimal("511.83"), "mo"),),
                            summary="", raw="{}", cancels=(CancelRequest("o-spy", "rotate"),))

    propose = ProposeDeps(broker=b, data=b, decider=D(), journal=journal, notifier=notifier, risk_config=cfg,
                          universe=("SPY", "AMD"), history_days=30, mode="paper", strategy_prompt="", halted=False)
    approve = ApproveDeps(broker=b, data=b, journal=journal, notifier=notifier, risk_config=cfg,
                          universe=("SPY", "AMD"), history_days=30, allowed_chat_id=CHAT)
    proposal = run_propose(propose)
    b.open_orders = []  # ...it filled in the meantime
    outcome = handle_tap(Tap(10, CHAT, "approve", proposal.id, "", "cq10"), approve)
    assert outcome.action == "submitted" and b.cancelled == [] and [o.symbol for o in b.submitted] == ["AMD"]
    assert any("Could not cancel" in s for s in notifier.sent)


def test_approver_reloads_config_before_submit(broker, risk_config):
    """A long-running approver must pick up a widened universe without a restart."""
    journal = SqliteJournal(":memory:")
    notifier = FakeNotifier()
    good = ProposedOrder("NVDA", Side.BUY, Decimal("1"), Decimal("180"), "trend up")
    propose_deps, approve_deps = _deps(broker, journal, notifier, risk_config, [good])
    proposal = run_propose(propose_deps)
    approve_deps.universe = ("AAPL",)  # stale: NVDA not in it -> would be rejected at submit
    approve_deps.reload_config = lambda: (risk_config, ("AAPL", "NVDA", "SPY", "TQQQ"), 30)  # fresh config
    outcome = handle_tap(Tap(11, CHAT, "approve", proposal.id, "", "cq11"), approve_deps)
    assert outcome.action == "submitted" and [o.symbol for o in broker.submitted] == ["NVDA"]
