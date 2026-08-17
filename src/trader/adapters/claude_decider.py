"""The brain, two ways.

``ClaudeDecider`` calls the Claude API directly and forces a strict JSON reply
via structured outputs, so parsing can't fail on prose.

``FileDecider`` reads a ``decision.json`` written by *someone else* — for
example a Claude Code / Cowork scheduled task that ran ``trader snapshot``,
reasoned about it, and wrote the file. Same contract, no API key needed.

Either way, the output is a ``Decision`` and the risk gate treats it identically.
The brain never talks to the broker.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import anthropic

from trader.domain.models import CancelRequest, Decision, MarketSnapshot, ProposedOrder, Side, to_json

log = logging.getLogger(__name__)

# The JSON shape the brain must return. Kept as a plain dict so it can be shown
# in docs and reused by FileDecider validation.
DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": (
                "2-4 sentences: your read of the market and the account today, written for the account owner."
            ),
        },
        "orders": {
            "type": "array",
            "description": "Zero or more limit orders. Empty is a perfectly good answer.",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "side": {"type": "string", "enum": ["buy", "sell"]},
                    "qty": {
                        "type": "string",
                        "description": "Number of shares as a decimal string, e.g. '2' or '0.75'.",
                    },
                    "limit_price": {"type": "string", "description": "Limit price as a decimal string, e.g. '181.20'."},
                    "rationale": {
                        "type": "string",
                        "description": "One or two sentences on why, referencing the data you were given.",
                    },
                },
                "required": ["symbol", "side", "qty", "limit_price", "rationale"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "orders"],
    "additionalProperties": False,
}
# Optional: cancels of open orders (by broker_order_id from the snapshot's open_orders list).
DECISION_SCHEMA["properties"]["cancels"] = {
    "type": "array",
    "description": "Open orders to cancel (ids from snapshot.open_orders), e.g. to rotate into a better setup.",
    "items": {
        "type": "object",
        "properties": {
            "broker_order_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["broker_order_id", "reason"],
        "additionalProperties": False,
    },
}

SYSTEM_PROMPT = """You are the decision step of a small, human-approved trading pipeline.

You will be given:
  1. A STRATEGY written by the account owner. Follow it. It is their money and their thesis.
  2. A SNAPSHOT of their account and the market as JSON: equity, cash, positions, open orders,
     current quotes, and per-name indicators (20/50-day SMA, 20-day high/low, 5/20-day returns, RSI,
     ATR, relative strength vs SPY, gap, range position, volume ratio) for a wide universe (~60 names),
     plus `regime` (risk_on/neutral/risk_off with breadth), `ranking` (momentum leaders/laggards
     shortlist) and `news` (recent headlines for held names and leaders). Read regime, then ranking,
     then the names you care about — not all 60 rows.

Your job: propose zero or more LIMIT orders for today, with a short rationale each, plus a short
summary for the owner. Rules you must respect (a code-enforced risk gate will also check them):
  * Only trade symbols in the universe or already held.
  * Limit prices must be close to the current price (within ~2%).
  * Never propose selling more than is held. Never propose short sales.
  * You may cancel open (unfilled) orders listed in snapshot.open_orders by broker_order_id, e.g.
    to rotate into a better setup or reprice; the capital they free is available to your buys.
  * Every order needs a rationale grounded in the numbers you were given. Do not invent prices,
    news, or events.
  * Quantities and prices are decimal strings.

Follow the STRATEGY's risk appetite and goals as written — it is the owner's brief. Be plain
and specific in the summary: what the data shows, what you are doing about it, and why.
"""


def parse_decision(data: dict[str, Any], raw: str = "") -> Decision:
    """Turn a dict matching DECISION_SCHEMA into a Decision. Raises ValueError if malformed."""
    orders: list[ProposedOrder] = []
    for i, o in enumerate(data.get("orders", []) or []):
        try:
            orders.append(
                ProposedOrder(
                    symbol=str(o["symbol"]).upper().strip(),
                    side=Side(str(o["side"]).lower()),
                    qty=Decimal(str(o["qty"])),
                    limit_price=Decimal(str(o["limit_price"])),
                    rationale=str(o.get("rationale", "")).strip(),
                )
            )
        except (KeyError, ValueError, InvalidOperation) as exc:
            raise ValueError(f"order #{i} is malformed: {exc}") from exc
    cancels: list[CancelRequest] = []
    for i, c in enumerate(data.get("cancels", []) or []):
        try:
            cancels.append(CancelRequest(str(c["broker_order_id"]).strip(), str(c.get("reason", "")).strip()))
        except (KeyError, TypeError) as exc:
            raise ValueError(f"cancel #{i} is malformed: {exc}") from exc
    summary = str(data.get("summary", "")).strip()
    return Decision(orders=tuple(orders), summary=summary, raw=raw or json.dumps(data), cancels=tuple(cancels))


class ClaudeDecider:
    """Calls Claude with the strategy + snapshot and returns a Decision."""

    def __init__(self, model: str = "claude-opus-5", client: anthropic.Anthropic | None = None) -> None:
        self.model = model
        # The SDK reads ANTHROPIC_API_KEY from the environment.
        self._client = client or anthropic.Anthropic()

    def decide(self, snapshot: MarketSnapshot, strategy_prompt: str) -> Decision:
        user_content = (
            "## STRATEGY (from the account owner)\n\n"
            f"{strategy_prompt.strip()}\n\n"
            "## SNAPSHOT (JSON)\n\n"
            f"{to_json(snapshot.to_json_dict())}\n\n"
            "Return your decision as JSON matching the required schema."
        )
        log.info("asking %s for a decision (%d chars of context)", self.model, len(user_content))
        response = self._client.messages.create(
            model=self.model,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
            output_config={"format": {"type": "json_schema", "schema": DECISION_SCHEMA}},
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("the model declined to answer (stop_reason=refusal)")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"model did not return valid JSON: {exc}\n{text[:500]}") from exc
        decision = parse_decision(data, raw=text)
        log.info(
            "decision: %d order(s); usage in=%s out=%s",
            len(decision.orders),
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        return decision


class FileDecider:
    """Reads a decision.json produced elsewhere (e.g. by a Cowork scheduled agent)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def decide(self, snapshot: MarketSnapshot, strategy_prompt: str) -> Decision:
        raw = self.path.read_text()
        return parse_decision(json.loads(raw), raw=raw)
