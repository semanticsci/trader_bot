"""Telegram adapter: send proposals with buttons, and read the taps back.

We use the plain Bot HTTP API with ``httpx`` — no framework, so you can see
every request. Two directions:

  * outbound (``NotifierPort``): sendMessage / editMessageText
  * inbound  (used by the approver): getUpdates long-polling — the Mac asks
    Telegram "anything new?" every ~20s. No public URL, no port forwarding.

Security model: the approver only honours taps/messages from ``chat_id``. A
stranger who finds the bot gets a polite refusal and is logged.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from trader.domain.models import Proposal, ProposalStatus

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"

APPROVE_PREFIX = "approve:"
SKIP_PREFIX = "skip:"
YES_WORDS = {"go", "go ahead", "yes", "y", "approve", "ok", "do it", "send it"}
NO_WORDS = {"no", "n", "skip", "cancel", "stop", "nope"}


@dataclass(frozen=True)
class Tap:
    """A normalized inbound event: either a button tap or a text reply."""

    update_id: int
    chat_id: str
    kind: str  # "approve" | "skip" | "text"
    proposal_id: str | None  # for button taps
    text: str  # raw text for text replies
    callback_query_id: str | None = None
    message_id: int | None = None


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, *, timeout: float = 30.0, max_attempts: int = 3) -> None:
        if not token or not chat_id:
            raise ValueError("Telegram token and chat_id are required (see .env.example).")
        self.token = token
        self.chat_id = str(chat_id)
        self.max_attempts = max_attempts
        self._http = httpx.Client(timeout=timeout)

    # ------------------------------------------------------------------ outbound

    def send_proposal(self, proposal: Proposal) -> int | None:
        text = format_proposal(proposal)
        buttons = None
        if proposal.accepted and proposal.status is ProposalStatus.PENDING:
            buttons = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Go ahead", "callback_data": f"{APPROVE_PREFIX}{proposal.id}"},
                        {"text": "❌ Skip", "callback_data": f"{SKIP_PREFIX}{proposal.id}"},
                    ]
                ]
            }
        payload: dict[str, Any] = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        if buttons:
            payload["reply_markup"] = buttons
        result = self._call("sendMessage", payload)
        return int(result["message_id"]) if result and "message_id" in result else None

    def send_text(self, text: str) -> None:
        self._call("sendMessage", {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"})

    def update_proposal_message(self, proposal: Proposal, footer: str) -> None:
        if proposal.telegram_message_id is None:
            return
        text = format_proposal(proposal) + f"\n\n<b>{_esc(footer)}</b>"
        # Editing removes the buttons because we omit reply_markup.
        self._call(
            "editMessageText",
            {
                "chat_id": self.chat_id,
                "message_id": proposal.telegram_message_id,
                "text": text,
                "parse_mode": "HTML",
            },
        )

    def answer_callback(self, callback_query_id: str, text: str) -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text[:190]})

    # ------------------------------------------------------------------ inbound

    def poll(self, offset: int | None, timeout_s: int = 20) -> list[Tap]:
        """Long-poll for updates. Returns normalized Taps (all chats — caller filters)."""
        payload: dict[str, Any] = {"timeout": timeout_s, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        try:
            updates = self._call("getUpdates", payload, timeout=timeout_s + 10) or []
        except httpx.HTTPError as exc:
            log.warning("telegram poll failed: %s", exc)
            return []
        return [t for t in (parse_update(u) for u in updates) if t is not None]

    def whoami(self) -> dict[str, Any]:
        return self._call("getMe", {}) or {}

    def recent_chat_ids(self) -> list[tuple[str, str]]:
        """(chat_id, first_name) for recent messages — helps a new user find their chat id."""
        updates = self._call("getUpdates", {"timeout": 0}) or []
        seen: dict[str, str] = {}
        for u in updates:
            msg = u.get("message") or {}
            chat = msg.get("chat") or {}
            if "id" in chat:
                seen[str(chat["id"])] = str(chat.get("first_name") or chat.get("title") or "?")
        return list(seen.items())

    # ------------------------------------------------------------------ plumbing

    def _call(self, method: str, payload: dict[str, Any], timeout: float | None = None) -> Any:
        """POST one Bot API method. Retries transient transport errors (connection resets,
        timeouts) a few times with backoff — a proposal that is journaled but never delivered
        would otherwise just expire unseen. Telegram-side errors (bad token, bad chat) are not
        retried. The token is in the URL, so we never log the URL."""
        url = API.format(token=self.token, method=method)
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = self._http.post(url, json=payload, timeout=timeout or self._http.timeout)
                data = resp.json()
            except httpx.TransportError as exc:  # network-level: reset, timeout, DNS
                last_exc = exc
                if attempt < self.max_attempts:
                    delay = 1.5 * attempt
                    log.warning("telegram %s: transport error (%s), retry %d/%d in %.1fs",
                                method, type(exc).__name__, attempt, self.max_attempts, delay)
                    time.sleep(delay)
                continue
            if not data.get("ok"):
                log.error("telegram %s failed: %s", method, data.get("description"))
                raise httpx.HTTPError(f"telegram {method}: {data.get('description')}")
            return data.get("result")
        raise httpx.HTTPError(f"telegram {method}: gave up after {self.max_attempts} attempts ({last_exc})")


# ---------------------------------------------------------------------- pure helpers (tested)


def parse_update(update: dict[str, Any]) -> Tap | None:
    """Normalize a raw Telegram update into a Tap. Pure; unit-tested."""
    uid = int(update.get("update_id", 0))
    cq = update.get("callback_query")
    if cq:
        data = str(cq.get("data", ""))
        chat_id = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
        msg_id = (cq.get("message") or {}).get("message_id")
        if data.startswith(APPROVE_PREFIX):
            return Tap(uid, chat_id, "approve", data[len(APPROVE_PREFIX):], "", cq.get("id"), msg_id)
        if data.startswith(SKIP_PREFIX):
            return Tap(uid, chat_id, "skip", data[len(SKIP_PREFIX):], "", cq.get("id"), msg_id)
        return None
    msg = update.get("message")
    if msg and "text" in msg:
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        return Tap(uid, chat_id, "text", None, str(msg["text"]).strip(), None, msg.get("message_id"))
    return None


def classify_text(text: str) -> str | None:
    """'go' -> 'approve', 'no' -> 'skip', anything else -> None."""
    t = text.strip().lower().rstrip("!.")
    if t in YES_WORDS:
        return "approve"
    if t in NO_WORDS:
        return "skip"
    return None


def format_proposal(p: Proposal) -> str:
    """Render a Proposal as Telegram HTML."""
    mode = "PAPER" if p.mode == "paper" else "🔴 LIVE"
    when = p.created_at.astimezone().strftime("%a %b %d, %H:%M")
    lines = [f"<b>Trading proposal — {when}</b>  [{mode}]"]
    if p.summary:
        lines.append(_esc(p.summary))
    if p.accepted:
        lines.append("")
        for i, o in enumerate(p.accepted, 1):
            lines.append(f"<b>{i}. {_esc(o.describe())}</b>")
            if o.rationale:
                lines.append(f"   <i>{_esc(o.rationale)}</i>")
    else:
        lines.append("")
        lines.append("<i>No orders passed the gate today.</i>")
    if p.rejected:
        lines.append("")
        lines.append(f"Gate rejected {len(p.rejected)}:")
        for r in p.rejected:
            lines.append(f" • {_esc(r.order.describe())} — {_esc('; '.join(r.reasons))}")
    exp = p.expires_at.astimezone().strftime("%H:%M")
    lines.append("")
    lines.append(f"Valid until {exp}. Reply <b>go</b> / <b>no</b> or tap below.  id:{p.id}")
    return "\n".join(lines)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
