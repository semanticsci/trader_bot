# How it works — the story of one day

This is the whole system explained as a narrative. Read it once, then read the code with it
in mind.

## 08:30 — the morning proposal (`trader propose`)

Something kicks off `trader propose` — a Claude Desktop scheduled task, a cron line, or you
typing it. It runs in one process and does five things in order:

### 1. Collect (`app/snapshot.py`)

It asks Alpaca two questions: *what does the account look like?* (equity, cash, positions,
yesterday's closing equity) and *what are prices doing?* for every symbol in the configured
universe plus anything you already hold — the current quote and the last ~60 daily bars.

From the bars it computes a few honest indicators (`domain/indicators.py`): 20- and 50-day
moving averages, 20-day high/low, 5- and 20-day returns, average volume. Nothing exotic;
enough that neither the model nor you has to squint at 60 numbers per symbol.

All of that becomes one immutable object, `MarketSnapshot`. It is what the brain sees and
what the journal keeps, verbatim, forever.

### 2. Decide (`adapters/claude_decider.py`)

The snapshot (as JSON) and your `STRATEGY.md` (as text) go to Claude with a system prompt
that says, in short: *here is the owner's thesis, here is the data, propose zero or more limit
orders with a rationale each, and be honest.* The response is forced into a strict JSON schema
(structured outputs), so parsing cannot fail on prose. Out comes a `Decision`: a list of
`ProposedOrder`s and a summary paragraph.

Two things to notice. First, the brain has **no access to the broker** — it can't place, cancel,
or even see orders; it only sees the snapshot we gave it. Second, the brain is
**replaceable**: `FileDecider` reads the same JSON from a file, so a Cowork agent (or you) can
be the brain instead of the API. The gate does not care who proposed.

### 3. Gate (`domain/risk.py`)

Every proposed order runs through the risk gate. The gate is plain Python with no I/O, so it's
fully unit-tested (`tests/test_risk_gate.py` has one test per rule). Rules include: symbol
must be in the universe or already held; not on the blocked list; limit price within a few
percent of the current price; single order under `$max_order_notional`; resulting position
under `max_position_pct` of equity; keep `min_cash_buffer_pct` in cash; no shorting; no
selling more than you own; at most N orders; and a **daily-loss circuit breaker** — if the
account is down more than X% vs. yesterday's close, no new buys today. Rules are cumulative
within a proposal (the third buy is judged after the first two are assumed to fill).

Every rejection carries a human-readable reason. That's not decoration: you'll see the reason
on your phone, and it's how you learn whether the gate or the strategy is the thing to adjust.

### 4. Journal (`adapters/sqlite_journal.py`)

A `Proposal` is created (short id, created/expires times, accepted orders, rejected orders
with reasons, the summary, the full snapshot, the raw model output) and written to SQLite.
`data/journal.db` is a single file. It is append-mostly: proposals never change except their
`status`.

### 5. Notify (`adapters/telegram_notifier.py`)

The proposal is rendered as a Telegram message with two inline buttons — **✅ Go ahead** and
**❌ Skip** — and sent to your chat. The message id is stored so the buttons can be removed
later. `trader propose` exits. **No order has been placed.**

## Whenever you look at your phone — the approver (`trader approve`)

The approver is a separate, long-running process on your Mac (installed as a launchd agent).
It has one job: turn your tap into orders — and refuse everyone and everything else.

It **long-polls** Telegram (`getUpdates`, 20-second timeout) — your Mac asks "anything new?"
over and over. There's no public URL, no webhook, no port to open. When an update arrives:

1. **Is it from you?** If the chat id doesn't match `.env`, it's logged and ignored.
2. **Which proposal, which action?** A button tap carries `approve:<id>` or `skip:<id>`. A text
   reply like "go" or "no" applies to the latest pending proposal.
3. **Is it still pending and not expired?** If it expired (default TTL 7 h from creation),
   the message is edited to say so and nothing is submitted. Stale limit prices are how people
   buy the top.
4. **Skip?** Status → `skipped`, buttons removed, done.
5. **Approve?** Status → `approved`, then `submit_proposal`:
   - checks the `HALT` kill-switch file,
   - takes a **fresh** snapshot and **re-runs the gate** — prices moved since 8:30 and the
     gate is cheap; an order that no longer passes is skipped and you're told why,
   - submits each survivor as a **DAY limit order** with a deterministic `client_order_id`
     (`<proposal>-<symbol>-<side>`), so if the process crashes mid-way and retries, the broker
     returns the existing order instead of creating a twin,
   - records each broker order in the journal,
   - edits the original message (buttons vanish, "✅ Approved" appears) and sends a
     confirmation with order ids.

That is the **only** code path in the project that calls `submit_limit_order`. Everything else
is read-only. This is deliberate and worth defending in code review.

## 16:15 and Sunday — reports (`trader report`)

`daily` shows equity and today's P&L, the proposal(s), what filled, and current positions.
`weekly` shows equity change vs. the first snapshot of the week, the benchmark's move over the
same window, proposal/approval counts, and whether the stated weekly target was met — with a
reminder that one week is noise. Neither report is allowed to spin.

## Why it's shaped like this — the architecture in one paragraph

`domain/` knows nothing about the outside world; it defines the vocabulary and the rules.
`ports.py` declares what the app needs (a broker, market data, a brain, a notifier, a
journal) as `Protocol`s. `adapters/` implement those protocols for real services — and a
`FakeBroker` implements them in memory. `app/` are the use cases, taking their dependencies as
plain arguments. `cli.py` wires real adapters in; tests wire fakes in. Swap Alpaca for
Robinhood, Claude for a rules engine, or Telegram for Slack, and only one file changes. This
is "hexagonal" / "ports and adapters" architecture, and it's the same shape as much larger
production systems — just small enough to hold in your head.

## What could still go wrong (be honest with yourself)

- **Data quality.** The free Alpaca feed is IEX-only. It's fine for daily decisions; it is not
  the consolidated tape. Prices in the snapshot may differ slightly from your broker app.
- **The model can be confidently wrong.** That's what the gate, the expiry, the re-gate, and
  *you* are for. Read the rationales. If they stop making sense, fix `STRATEGY.md`.
- **The Mac was asleep.** Then your tap waits until it wakes. If the proposal expired in the
  meantime, that's the system working.
- **You will be tempted to raise the limits after a good week.** Read the journal first.
