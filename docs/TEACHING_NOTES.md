# Teaching notes — for reading this repo together

A suggested path through the project for someone learning to program *and* learning how
markets and risk work. Each session is an hour or so. Do them in order; each builds on the last.

## Session 1 — What is this thing? (no code)

- Read `README.md` and `docs/HOW_IT_WORKS.md`.
- Draw the pipeline on paper from memory. Where does a human sit? Where does money move?
- Discussion: *why* is "the model proposes, the code disposes" the rule? What goes wrong if
  the model could place orders directly? (Answer in one word: accountability.)
- Look at `STRATEGY.md`. Notice it says the 5%/week target is unrealistic. Compute
  1.05^52 on a calculator. Talk about compounding, and about why "unrealistic target" and
  "still worth building" can both be true.

## Session 2 — Vocabulary (`domain/models.py`)

- Read every dataclass. For each, ask: what real thing is this?
- Why `Decimal` and not `float` for money? Try `0.1 + 0.2` in Python. Then try it with
  `Decimal("0.1") + Decimal("0.2")`.
- Why is `Proposal` the only mutable one? What is a "lifecycle"?
- Exercise: add a `notes: str` field to `ProposedOrder`, run the tests, see what breaks, fix it.

## Session 3 — Rules and proof (`domain/risk.py`, `tests/test_risk_gate.py`)

- Read one rule, then its test. Repeat.
- Each test is Arrange / Act / Assert. Point at the three parts.
- Exercise: add a rule — "no more than one order per symbol per proposal" — and a test that
  fails before you write the rule and passes after. That's test-driven development.
- Discussion: the daily-loss breaker blocks buys but allows sells. Why? What would happen if
  it blocked both?

## Session 4 — Interfaces (`ports.py`, `adapters/fake_broker.py`)

- What is a `Protocol`? Why does the app talk to `BrokerPort` instead of `AlpacaBroker`?
- Read `FakeBroker`. It's a "broker" that never touches the internet. Why is that useful?
- Exercise: write a `FakeDecider` that always proposes buying 1 share of the symbol with the
  highest 20-day return. Wire it into `run_propose` in a test. (Look at `ScriptedDecider` in
  `tests/test_pipeline.py` for the shape.)

## Session 5 — The two halves (`app/propose.py`, `app/approve.py`)

- Trace `run_propose` line by line. Where does the snapshot get stored? Why store the whole
  thing and not just the orders?
- Trace `handle_tap`. List every way it can say "no". Which are about the *user* (wrong chat)
  and which about *time* (expired) and which about *state* (already skipped)?
- Why gate twice? Try to think of a case where an order passes at 8:30 and fails at 10:00.
- Discussion: `client_order_id` and idempotency. What if the laptop lost Wi-Fi right after
  Alpaca accepted the order but before we saved it? Run
  `test_idempotent_resubmit_does_not_duplicate` and explain it.

## Session 6 — Talking to the phone (`adapters/telegram_notifier.py`)

- Open <https://core.telegram.org/bots/api> next to the code. Find `sendMessage`,
  `getUpdates`, `answerCallbackQuery`, `editMessageText`.
- What is "long polling"? Why don't we need a server or a public URL?
- Exercise: add a third button, "🕐 Remind me in 1h". What would the approver need to do?
  (You don't have to build it — design it.)

## Session 7 — The journal (`adapters/sqlite_journal.py`)

- Open `data/journal.db` with the `sqlite3` CLI or a GUI. `SELECT id, status, summary FROM proposals;`
- Discussion: "Was the model right?" is a bad question. Better questions: was the rationale
  consistent with the data? Did the gate reject things that would have lost money? Did we
  approve things we shouldn't have? Which of these can the journal answer?
- Exercise: write a script that prints, for each proposal, the accepted orders and what the
  price of each symbol did over the following 5 days. That's a (crude) backtest.

## Session 8 — Run it for real (paper)

- Follow `docs/SETUP.md`. Get a proposal on a phone. Approve one. Skip one. Halt. Resume.
- Watch `data/approver.log` while tapping. Match log lines to code lines.
- Rewrite `STRATEGY.md` in your own words. Run `trader propose --dry-run` before and after.
  Did the proposals change? Were the new ones better-reasoned, or just different?

## Bigger projects

- **A second broker.** Implement `BrokerPort` for Robinhood (or Interactive Brokers, or a
  crypto exchange). Nothing outside `adapters/` and `cli.py` should change.
- **A backtester.** Replay journaled snapshots through the brain with a new `STRATEGY.md`,
  gate them, and simulate fills at the limit price. Compare against just holding SPY.
- **A dashboard.** Read the journal, chart equity, mark proposals on the chart, colour by
  approved/skipped/rejected.
- **A second channel.** Slack or Discord notifier + poller behind the same `NotifierPort`.
- **Better data.** Add a news headline source to the snapshot. Then argue about whether the
  brain should be allowed to see it.

## The lessons underneath the code

1. Put the dangerous action in one small place and make everything else read-only.
2. Never trust a single check. Gate, expire, re-gate, kill switch, human.
3. Every "no" needs a reason a person can read.
4. Keep receipts (the journal). Memory lies; SQLite doesn't.
5. Targets are for honesty, not for chasing.
