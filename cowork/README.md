# Cowork / Claude Code scheduled tasks

These are the prompts for the three scheduled tasks that run the *proposal* side of the
pipeline from Claude Desktop (Cowork). They are stored here so they're versioned and so anyone
can recreate them. **They are not created automatically** — create them once `.env` is filled
in and `trader propose --dry-run` works.

| Task | Cron (local) | Prompt file | What it does |
|---|---|---|---|
| `trading-proposal-open` | `45 9 * * 1-5` | `proposal.md` | runs `trader propose` after the open settles → Telegram proposal |
| `trading-proposal-midday` | `30 12 * * 1-5` | `proposal.md` | same, midday check-in |
| `trading-proposal-close` | `0 15 * * 1-5` | `proposal.md` | same, before the close (proposal TTL is 3 h, so it dies at 18:00) |
| `trading-eod-summary` | `15 16 * * 1-5` | `eod-summary.md` | runs `trader report daily --send` |
| `trading-weekly-report` | `0 18 * * 0` | `weekly-report.md` | runs `trader report weekly --send` and critiques STRATEGY.md |

Facts about Cowork scheduled tasks worth knowing:

* They run **while the Claude Desktop app is open** and the Mac is awake. If it's closed at
  8:30, the task runs on next launch. Set the Mac to wake before 8:30 on weekdays if you
  want the proposal on time (System Settings → Energy / `pmset` — that's a system setting,
  so it's yours to change).
* Each run starts fresh with no memory of past chats — that's why the prompts are
  self-contained and everything they need lives in this repo.
* Where the tasks live on disk: `~/.claude/scheduled-tasks/<task-id>/SKILL.md`.
* Times are America/New_York (the Mac's timezone). US market hours are 09:30–16:00 ET.
  The three windows deliberately skip the first 15 minutes (noisy) and stop an hour before close.

## The one thing these tasks never do

They never call `trader approve` and never submit orders. That's the approver, a separate
launchd service under your account (see `../launchd/`). The scheduled task's job ends when
the proposal is on your phone.

## Two ways to be the brain

1. **`trader propose`** — the code calls Claude via the API (`ANTHROPIC_API_KEY` needed).
   Simple, and it works without Cowork — this is what your sons would run from cron.
2. **Agent-as-brain** — the scheduled task itself reads `trader snapshot` + `STRATEGY.md`,
   writes `data/decision.json`, then runs `trader propose --decision data/decision.json`.
   No API key needed; the reasoning happens in the Cowork agent. `proposal.md` describes both
   and picks automatically based on whether `ANTHROPIC_API_KEY` is set.
