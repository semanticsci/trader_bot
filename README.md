# trading-agent

An **educational, human-approved, LLM-assisted trading pipeline**, small enough to read in an
afternoon and honest enough to learn from.

Three times each trading day it looks at a small brokerage account and a handful of stocks, asks
Claude for a plan, runs that plan through a hard-coded risk gate, and sends the survivors to
your phone. **Nothing happens until you tap "Go ahead."** Then a tiny service on your Mac
places the orders, and everything — what the model saw, what it said, what the gate blocked,
what filled — goes into a journal you can study later.

```
   09:45/12:30/15:00      +1 min              +1 min              whenever you look         seconds later
┌──────────┐   ┌───────────┐   ┌────────────────┐   ┌────────────────────┐   ┌────────────────┐
│ collect  │──▶│  decide   │──▶│  risk gate     │──▶│  Telegram          │──▶│  approver      │
│ account, │   │ (Claude)  │   │ (code — LLM    │   │  "Go ahead / Skip" │   │ (your Mac)     │
│ quotes,  │   │ proposes  │   │  cannot bypass)│   │  YOU decide        │   │ re-gates,      │
│ history  │   │ orders    │   │                │   │                    │   │ submits to     │
└──────────┘   └───────────┘   └────────────────┘   └────────────────────┘   │ Alpaca         │
      └────────────────┴──────────────────┴─────────────────────┴───────────▶│ journal (SQLite)│
                                                                              └────────────────┘
```

> **This is a learning project.** It runs on a *paper* (fake-money) account by default and
> makes you jump through a deliberate hoop to point it at real money. It is not financial
> advice, it does not promise returns, and its own strategy file says so. If you're here to
> get rich, you're in the wrong repo. If you're here to understand how a disciplined,
> auditable, human-in-the-loop trading system is built — welcome.

## What you'll learn from this repo

- How to structure a program so the dangerous part (placing orders) is tiny, isolated, and
  testable — *hexagonal architecture* in ~1,500 lines.
- Why "the model proposes, the code disposes" is the only sane way to let an LLM near money.
- What a **risk gate** is and why every rule needs a plain-English reason.
- Why you gate **twice** (at proposal time and again at submit time).
- Idempotency, kill switches, expiry, and other boring things that keep you solvent.
- How to keep a **journal** so you can tell luck from skill.
- How to build a Telegram approval flow with no server, no webhook, no port forwarding.

## Quick start (paper account, ~20 minutes)

```bash
git clone <this repo> && cd trading-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest            # everything should pass, offline

cp .env.example .env        # then fill it in — see docs/SETUP.md
trader telegram-test        # find your chat id, get a hello on your phone
trader status               # talk to Alpaca paper account
trader propose --dry-run    # full cycle, prints the proposal, sends nothing
trader propose              # ...now it sends. Tap Go ahead / Skip.
./launchd/install_approver.sh   # your Mac now honours your taps
```

Full walk-through with screenshots-in-words: [docs/SETUP.md](docs/SETUP.md).
Want Claude Code to do most of it with you? [docs/USING_CLAUDE_CODE.md](docs/USING_CLAUDE_CODE.md).

## Repository map

```
STRATEGY.md              ← YOUR words. Given to the brain at every check-in. Rewrite freely.
config.toml              ← universe + risk limits + goal. Enforced in code.
.env(.example)           ← secrets. Never committed.
src/trader/
  domain/                ← pure Python: models, indicators, risk gate.  START HERE.
  ports.py               ← the interfaces (Protocols) the app needs from the world
  adapters/              ← Alpaca, Claude, Telegram, SQLite, and a FakeBroker for tests
  app/                   ← use cases: snapshot, propose, approve, report
  cli.py                 ← `trader ...`
tests/                   ← 51 offline tests; the risk gate has one per rule
cowork/                  ← prompts for the three Claude Desktop scheduled tasks
launchd/                 ← the approver as a macOS background service
docs/                    ← how it works, setup, teaching notes, using Claude Code on this repo
```

## Reading order for a newcomer

1. `docs/HOW_IT_WORKS.md` — the story of one day, end to end.
2. `src/trader/domain/models.py` — the vocabulary.
3. `src/trader/domain/risk.py` + `tests/test_risk_gate.py` — the rules and proof they work.
4. `src/trader/app/propose.py` then `app/approve.py` — the two halves.
5. `src/trader/adapters/telegram_notifier.py` — how a phone tap becomes a Python event.
6. Everything else.

## Commands

| Command | What it does |
|---|---|
| `trader propose [--dry-run] [--decision f.json]` | one check-in: collect → decide → gate → journal → send the proposal |
| `trader approve [--once]` | the approver loop (normally run by launchd) |
| `trader snapshot` | print what the brain would see, as JSON |
| `trader status` | account, positions, pending proposal |
| `trader report daily\|weekly [--send]` | honest reports |
| `trader telegram-test` | bot check + chat id discovery |
| `trader halt` / `trader resume` | kill switch (creates/removes the `HALT` file) |

## Safety properties (and where each lives)

| Property | Where |
|---|---|
| The LLM never talks to the broker | `adapters/claude_decider.py` has no broker import; only `app/approve.py` calls `submit_limit_order` |
| Every order passes a code-enforced gate, with a reason on rejection | `domain/risk.py`, `tests/test_risk_gate.py` |
| Gate runs again on fresh prices at submit time | `app/approve.py::submit_proposal` |
| Only your Telegram chat can approve | `app/approve.py::handle_tap` (chat id check) |
| Proposals expire | `Proposal.expires_at`, `risk.proposal_ttl_hours` |
| Retries can't double-submit | `client_order_id` idempotency, `tests/test_pipeline.py::test_idempotent_resubmit_does_not_duplicate` |
| Kill switch | `HALT` file — checked at proposal *and* submit time |
| Real money needs a magic phrase | `config.py::LIVE_CONFIRM_PHRASE` |
| No secrets in code or logs | only `config.py` reads env; adapters get values passed in |

## Extending it (good first projects)

- Add a Robinhood adapter (`adapters/robinhood_broker.py`) implementing `BrokerPort` — nothing else changes.
- Add a rule to the gate (e.g. "no buys in the last 30 minutes before close") + a test.
- Add an indicator (RSI, ATR) to `domain/indicators.py` and expose it in `Indicators`.
- Backtest: replay journaled snapshots through a *different* STRATEGY.md and compare decisions.
- Replace Telegram with Discord or Slack by implementing `NotifierPort` + a poller.

## License

MIT — see `LICENSE`. Use it, learn from it, break it, fix it.
