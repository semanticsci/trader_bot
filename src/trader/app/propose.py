"""Use case: the morning cycle. collect -> decide -> gate -> journal -> notify.

This never places an order. It ends when the human has a message on their
phone. The approver (``approve.py``) is a separate process that reacts to
their tap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from trader.domain import risk
from trader.domain.models import Proposal, ProposalStatus, RiskConfig, utcnow
from trader.ports import BrokerPort, DeciderPort, JournalPort, MarketDataPort, NotifierPort

from .snapshot import take_snapshot

log = logging.getLogger(__name__)


@dataclass
class ProposeDeps:
    """Everything ``run_propose`` needs, passed in explicitly (no globals)."""

    broker: BrokerPort
    data: MarketDataPort
    decider: DeciderPort
    journal: JournalPort
    notifier: NotifierPort | None  # None = don't send (dry run)
    risk_config: RiskConfig
    universe: tuple[str, ...]
    history_days: int
    mode: str
    strategy_prompt: str
    halted: bool = False


def run_propose(deps: ProposeDeps) -> Proposal:
    """Run one full proposal cycle and return the resulting Proposal."""
    snapshot = take_snapshot(deps.broker, deps.data, deps.universe, deps.history_days, deps.risk_config.capital_cap)

    decision = deps.decider.decide(snapshot, deps.strategy_prompt)
    log.info("brain proposed %d order(s)", len(decision.orders))

    ok_cancels, bad_cancels = risk.evaluate_cancels(decision.cancels, snapshot)
    for c, why in bad_cancels:
        log.info("  rejected cancel %s: %s", c.describe(), why)
    results = risk.evaluate(decision.orders, snapshot, deps.risk_config, halted=deps.halted, cancels=ok_cancels)
    log.info("gate: %s", risk.summarize(results))
    for r in results:
        if not r.accepted:
            log.info("  rejected %s: %s", r.order.describe(), "; ".join(r.reasons))

    accepted = tuple(r.order for r in results if r.accepted)
    rejected = tuple(r for r in results if not r.accepted)
    if deps.halted:
        ok_cancels = []  # nothing at all while halted; the human can cancel by hand
    now = utcnow()
    proposal = Proposal(
        id=Proposal.new_id(),
        created_at=now,
        expires_at=now + deps.risk_config.proposal_ttl,
        mode=deps.mode,
        accepted=accepted,
        rejected=rejected,
        summary=decision.summary,
        status=ProposalStatus.PENDING if (accepted or ok_cancels) else ProposalStatus.EMPTY,
        snapshot=snapshot.to_json_dict(),
        decision_raw=decision.raw,
        cancels=tuple(ok_cancels),
        rejected_cancels=tuple(bad_cancels),
    )

    deps.journal.save_proposal(proposal)
    deps.journal.log_event(
        "proposal_created",
        {"proposal_id": proposal.id, "accepted": len(accepted), "rejected": len(rejected), "mode": deps.mode},
    )

    if deps.notifier is not None:
        msg_id = deps.notifier.send_proposal(proposal)
        proposal.telegram_message_id = msg_id
        deps.journal.save_proposal(proposal)
        log.info("sent proposal %s to telegram (message_id=%s)", proposal.id, msg_id)
    else:
        log.info("dry run — not sending proposal %s", proposal.id)

    return proposal
