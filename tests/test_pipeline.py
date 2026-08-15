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
