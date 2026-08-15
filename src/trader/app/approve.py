"""Use case: the approver. Waits for the human's tap, then — and only then — submits.

This is the only code path in the whole project that calls
``broker.submit_limit_order``. It runs as a long-lived process on the owner's
machine (see ``launchd/``). It:

  1. long-polls Telegram for taps / "go" / "no" replies,
  2. ignores anything not from the configured chat id,
  3. loads the proposal, checks it is still pending and not expired,
  4. RE-RUNS the risk gate against a *fresh* account snapshot (prices moved
     since 8:30 — the gate is cheap, so gate twice),
  5. submits each surviving order with an idempotent client_order_id,
  6. journals everything and edits the Telegram message so the buttons vanish.

Everything about step 3–5 is in ``handle_tap`` which is pure enough to unit-test
with a FakeBroker.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from trader.adapters.telegram_notifier import Tap, TelegramNotifier, classify_text
from trader.domain import risk
from trader.domain.models import Proposal, ProposalStatus, RiskConfig, utcnow
from trader.ports import BrokerPort, JournalPort, MarketDataPort

from .snapshot import take_snapshot

log = logging.getLogger(__name__)

STATE_OFFSET_KEY = "telegram_update_offset"


@dataclass
class ApproveDeps:
    broker: BrokerPort
    data: MarketDataPort
    journal: JournalPort
    notifier: TelegramNotifier
    risk_config: RiskConfig
    universe: tuple[str, ...]
    history_days: int
    allowed_chat_id: str
    halted_check: object = field(default=lambda: False)  # callable() -> bool; injected for tests

    def is_halted(self) -> bool:
        fn = self.halted_check
        return bool(fn()) if callable(fn) else False


@dataclass
class TapOutcome:
    """What happened when we handled one tap — for logs, tests, and the reply."""

    proposal_id: str | None
    action: str  # "submitted" | "skipped" | "ignored" | "expired" | "not_found" | "nothing_pending" | "unauthorized"
    submitted: int = 0
    gate_rejected: int = 0
    message: str = ""


def run_approver(deps: ApproveDeps, *, once: bool = False, poll_timeout_s: int = 20) -> None:
    """Loop forever (or once), handling taps as they arrive."""
    offset_raw = deps.journal.get_state(STATE_OFFSET_KEY)
    offset = int(offset_raw) if offset_raw else None
    log.info("approver started (chat_id=%s, offset=%s)", deps.allowed_chat_id, offset)
    while True:
        taps = deps.notifier.poll(offset, timeout_s=poll_timeout_s)
        for tap in taps:
            offset = tap.update_id + 1
            deps.journal.set_state(STATE_OFFSET_KEY, str(offset))
            try:
                outcome = handle_tap(tap, deps)
                log.info("tap %s -> %s %s", tap.kind, outcome.action, outcome.message)
            except Exception:  # noqa: BLE001 — a bad tap must not kill the daemon
                log.exception("error handling tap %s", tap)
                try:
                    deps.notifier.send_text("⚠️ Error handling your reply — check the approver log.")
                except Exception:  # noqa: BLE001
                    log.exception("could not report error to telegram")
        expire_stale(deps)
        if once:
            return
        if not taps:
            time.sleep(1)  # be gentle if Telegram returned instantly


def handle_tap(tap: Tap, deps: ApproveDeps) -> TapOutcome:
    """Decide what a tap means and act on it. Sends replies via deps.notifier."""
    if tap.chat_id != deps.allowed_chat_id:
        deps.journal.log_event("unauthorized_tap", {"chat_id": tap.chat_id, "kind": tap.kind})
        log.warning("ignoring tap from unauthorized chat %s", tap.chat_id)
        return TapOutcome(None, "unauthorized")

    # Work out which proposal and which action.
    if tap.kind in ("approve", "skip"):
        action = tap.kind
        proposal = deps.journal.get_proposal(tap.proposal_id or "")
        if proposal is None:
            _ack(deps, tap, "Unknown proposal.")
            return TapOutcome(tap.proposal_id, "not_found")
    else:
        action = classify_text(tap.text) or ""
        if not action:
            return TapOutcome(None, "ignored", message=f"text {tap.text!r} not a command")
        proposal = deps.journal.latest_pending()
        if proposal is None:
            deps.notifier.send_text("Nothing pending to approve right now.")
            return TapOutcome(None, "nothing_pending")

    if proposal.status is not ProposalStatus.PENDING:
        _ack(deps, tap, f"Already {proposal.status.value}.")
        return TapOutcome(proposal.id, "ignored", message=f"status was {proposal.status.value}")

    if proposal.is_expired():
        proposal.status = ProposalStatus.EXPIRED
        deps.journal.save_proposal(proposal)
        deps.notifier.update_proposal_message(proposal, "⌛ Expired — nothing was submitted.")
        _ack(deps, tap, "That proposal has expired.")
        return TapOutcome(proposal.id, "expired")

    if action == "skip":
        proposal.status = ProposalStatus.SKIPPED
        deps.journal.save_proposal(proposal)
        deps.journal.log_event("proposal_skipped", {"proposal_id": proposal.id})
        deps.notifier.update_proposal_message(proposal, "❌ Skipped — nothing was submitted.")
        _ack(deps, tap, "Skipped.")
        return TapOutcome(proposal.id, "skipped")

    # ---- approve ----
    proposal.status = ProposalStatus.APPROVED
    deps.journal.save_proposal(proposal)
    _ack(deps, tap, "On it…")
    return submit_proposal(proposal, deps)


def submit_proposal(proposal: Proposal, deps: ApproveDeps) -> TapOutcome:
    """Gate again on fresh data, submit survivors, journal, report back."""
    if deps.is_halted():
        proposal.status = ProposalStatus.SKIPPED
        deps.journal.save_proposal(proposal)
        deps.notifier.update_proposal_message(proposal, "🛑 HALT file present — nothing was submitted.")
        return TapOutcome(proposal.id, "skipped", message="halted")

    fresh = take_snapshot(deps.broker, deps.data, deps.universe, deps.history_days)
    results = risk.evaluate(proposal.accepted, fresh, deps.risk_config, halted=False)

    submitted: list[str] = []
    rejected: list[str] = []
    for r in results:
        if not r.accepted:
            rejected.append(f"{r.order.describe()} — {'; '.join(r.reasons)}")
            deps.journal.log_event(
                "order_rejected_at_submit",
                {"proposal_id": proposal.id, "order": r.order.describe(), "reasons": list(r.reasons)},
            )
            continue
        # Idempotent id: if the process crashes after submitting and retries, the broker
        # returns the same order instead of creating a duplicate.
        client_id = f"{proposal.id}-{r.order.symbol}-{r.order.side.value}"[:48]
        try:
            bo = deps.broker.submit_limit_order(r.order, client_order_id=client_id)
        except Exception as exc:  # noqa: BLE001 — report per-order failure, keep going
            log.exception("broker rejected %s", r.order.describe())
            rejected.append(f"{r.order.describe()} — broker error: {exc}")
            deps.journal.log_event("order_broker_error", {"proposal_id": proposal.id, "error": str(exc)})
            continue
        deps.journal.record_broker_order(proposal.id, bo)
        submitted.append(f"{r.order.describe()} → {bo.status} (id {bo.broker_order_id[:8]}…)")

    proposal.status = ProposalStatus.SUBMITTED if submitted else ProposalStatus.SKIPPED
    deps.journal.save_proposal(proposal)
    deps.journal.log_event(
        "proposal_submitted",
        {"proposal_id": proposal.id, "submitted": len(submitted), "rejected": len(rejected)},
    )

    lines = []
    if submitted:
        lines.append(f"✅ Submitted {len(submitted)}:")
        lines += [f" • {s}" for s in submitted]
    if rejected:
        lines.append(f"⚠️ Not submitted {len(rejected)} (re-check on fresh prices):")
        lines += [f" • {s}" for s in rejected]
    if not submitted and not rejected:
        lines.append("Nothing to submit.")
    footer = "✅ Approved" if submitted else "⚠️ Approved, but nothing could be submitted"
    deps.notifier.update_proposal_message(proposal, footer)
    deps.notifier.send_text("\n".join(lines))
    return TapOutcome(proposal.id, "submitted" if submitted else "skipped", len(submitted), len(rejected))


def expire_stale(deps: ApproveDeps) -> None:
    """Mark the latest pending proposal expired if its time has passed."""
    p = deps.journal.latest_pending()
    if p is not None and p.is_expired(utcnow()):
        p.status = ProposalStatus.EXPIRED
        deps.journal.save_proposal(p)
        deps.journal.log_event("proposal_expired", {"proposal_id": p.id})
        try:
            deps.notifier.update_proposal_message(p, "⌛ Expired — nothing was submitted.")
        except Exception:  # noqa: BLE001
            log.exception("could not edit expired message")


def _ack(deps: ApproveDeps, tap: Tap, text: str) -> None:
    """Acknowledge a button tap (removes the spinner on the phone)."""
    if tap.callback_query_id:
        try:
            deps.notifier.answer_callback(tap.callback_query_id, text)
        except Exception:  # noqa: BLE001
            log.debug("answerCallbackQuery failed (harmless)")
