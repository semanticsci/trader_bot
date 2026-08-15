"""SQLite journal: the system's memory.

Every proposal (with the full snapshot the brain saw and its raw answer), every
broker order, and every notable event is stored here. Later you can ask
questions like "did the brain actually add anything over just holding SPY?" —
without a journal, that question is unanswerable.

Schema is created on first use. It's a single file; back it up by copying it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trader.domain.models import (
    BrokerOrder,
    GateResult,
    Proposal,
    ProposalStatus,
    ProposedOrder,
    Side,
    to_json,
    utcnow,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id                  TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    expires_at          TEXT NOT NULL,
    mode                TEXT NOT NULL,
    status              TEXT NOT NULL,
    telegram_message_id INTEGER,
    summary             TEXT NOT NULL,
    accepted_json       TEXT NOT NULL,
    rejected_json       TEXT NOT NULL,
    snapshot_json       TEXT NOT NULL,
    decision_raw        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS broker_orders (
    broker_order_id  TEXT PRIMARY KEY,
    proposal_id      TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,
    qty              TEXT NOT NULL,
    limit_price      TEXT,
    status           TEXT NOT NULL,
    filled_qty       TEXT NOT NULL,
    filled_avg_price TEXT,
    submitted_at     TEXT,
    filled_at        TEXT,
    recorded_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SqliteJournal:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), detect_types=0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------ proposals

    def save_proposal(self, p: Proposal) -> None:
        self._conn.execute(
            """INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 status=excluded.status,
                 telegram_message_id=excluded.telegram_message_id""",
            (
                p.id,
                p.created_at.isoformat(),
                p.expires_at.isoformat(),
                p.mode,
                p.status.value,
                p.telegram_message_id,
                p.summary,
                to_json([_order_dict(o) for o in p.accepted]),
                to_json(
                    [
                        {"order": _order_dict(r.order), "accepted": r.accepted, "reasons": list(r.reasons)}
                        for r in p.rejected
                    ]
                ),
                json.dumps(p.snapshot, sort_keys=True),
                p.decision_raw,
            ),
        )

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        row = self._conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        return _row_to_proposal(row) if row else None

    def latest_pending(self) -> Proposal | None:
        row = self._conn.execute(
            "SELECT * FROM proposals WHERE status=? ORDER BY created_at DESC LIMIT 1",
            (ProposalStatus.PENDING.value,),
        ).fetchone()
        return _row_to_proposal(row) if row else None

    def list_proposals(self, since: datetime) -> list[Proposal]:
        rows = self._conn.execute(
            "SELECT * FROM proposals WHERE created_at>=? ORDER BY created_at", (since.isoformat(),)
        ).fetchall()
        return [_row_to_proposal(r) for r in rows]

    # ------------------------------------------------------------------ orders

    def record_broker_order(self, proposal_id: str, o: BrokerOrder) -> None:
        self._conn.execute(
            """INSERT INTO broker_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(broker_order_id) DO UPDATE SET
                 status=excluded.status, filled_qty=excluded.filled_qty,
                 filled_avg_price=excluded.filled_avg_price, filled_at=excluded.filled_at""",
            (
                o.broker_order_id,
                proposal_id,
                o.symbol,
                o.side.value,
                str(o.qty),
                str(o.limit_price) if o.limit_price is not None else None,
                o.status,
                str(o.filled_qty),
                str(o.filled_avg_price) if o.filled_avg_price is not None else None,
                o.submitted_at.isoformat() if o.submitted_at else None,
                o.filled_at.isoformat() if o.filled_at else None,
                utcnow().isoformat(),
            ),
        )

    def list_broker_orders(self, since: datetime) -> list[BrokerOrder]:
        rows = self._conn.execute(
            "SELECT * FROM broker_orders WHERE recorded_at>=? ORDER BY recorded_at", (since.isoformat(),)
        ).fetchall()
        return [
            BrokerOrder(
                broker_order_id=r["broker_order_id"],
                symbol=r["symbol"],
                side=Side(r["side"]),
                qty=Decimal(r["qty"]),
                limit_price=Decimal(r["limit_price"]) if r["limit_price"] else None,
                status=r["status"],
                filled_qty=Decimal(r["filled_qty"]),
                filled_avg_price=Decimal(r["filled_avg_price"]) if r["filled_avg_price"] else None,
                submitted_at=_dt(r["submitted_at"]),
                filled_at=_dt(r["filled_at"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ events & state

    def log_event(self, kind: str, payload: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO events (ts, kind, payload) VALUES (?,?,?)",
            (utcnow().isoformat(), kind, to_json(payload)),
        )

    def list_events(self, since: datetime) -> list[tuple[datetime, str, dict[str, Any]]]:
        rows = self._conn.execute(
            "SELECT ts, kind, payload FROM events WHERE ts>=? ORDER BY id", (since.isoformat(),)
        ).fetchall()
        return [(datetime.fromisoformat(r["ts"]), r["kind"], json.loads(r["payload"])) for r in rows]

    def get_state(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# ---------------------------------------------------------------------- (de)serialization


def _order_dict(o: ProposedOrder) -> dict[str, Any]:
    return {
        "symbol": o.symbol,
        "side": o.side.value,
        "qty": str(o.qty),
        "limit_price": str(o.limit_price),
        "rationale": o.rationale,
    }


def _dict_order(d: dict[str, Any]) -> ProposedOrder:
    return ProposedOrder(
        symbol=d["symbol"],
        side=Side(d["side"]),
        qty=Decimal(d["qty"]),
        limit_price=Decimal(d["limit_price"]),
        rationale=d.get("rationale", ""),
    )


def _row_to_proposal(r: sqlite3.Row) -> Proposal:
    return Proposal(
        id=r["id"],
        created_at=datetime.fromisoformat(r["created_at"]),
        expires_at=datetime.fromisoformat(r["expires_at"]),
        mode=r["mode"],
        accepted=tuple(_dict_order(d) for d in json.loads(r["accepted_json"])),
        rejected=tuple(
            GateResult(order=_dict_order(d["order"]), accepted=d["accepted"], reasons=tuple(d["reasons"]))
            for d in json.loads(r["rejected_json"])
        ),
        summary=r["summary"],
        status=ProposalStatus(r["status"]),
        telegram_message_id=r["telegram_message_id"],
        snapshot=json.loads(r["snapshot_json"]),
        decision_raw=r["decision_raw"],
    )


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None
