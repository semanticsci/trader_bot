# Using Claude Code on this repo

This project was built *with* Claude Code and is meant to be run and extended *with* Claude
Code. This page is for someone (hi, sons) who has cloned it and wants Claude to do most of the
work with them. It assumes you have Claude Code (CLI, desktop app, or IDE extension) and can
open a folder in it.

## First five minutes

```bash
git clone https://github.com/semanticsci/trader_bot.git ~/Development/trading-agent
cd ~/Development/trading-agent
claude          # or open the folder in the Claude desktop app
```

Claude reads `CLAUDE.md` in the repo root automatically. It tells Claude the layout, the rules
(paper first, the LLM never places orders, keys never in code), and what it must not do (approve
trades for you, weaken the live-money interlock, rewrite `STRATEGY.md` unasked). You don't need
to repeat any of that.

Good first prompts, in order:

1. *"Read README.md and docs/HOW_IT_WORKS.md and explain this project to me like I'm new to
   trading and to Python. Then quiz me on where the dangerous part of the code lives."*
2. *"Set up the venv, install the project, and run the tests. Tell me what the tests prove."*
3. *"Walk me through docs/SETUP.md step by step. Open the pages I need in the browser. I'll
   create the accounts and paste the keys into .env myself — you don't touch the keys."*
4. *"Run `trader propose --dry-run` and explain every line of the output — what the snapshot
   contained, what the brain proposed, what the gate did and why."*
5. *"Help me rewrite STRATEGY.md in my own words. Ask me questions first; don't write my
   opinions for me."*

## Things Claude will do happily

- Explain any file, function, or test. Draw the pipeline. Quiz you.
- Set up the environment, run tests, run `trader status` / `snapshot` / `report`.
- Navigate the browser to Alpaca / BotFather / the Anthropic console and tell you where to click.
- Write the `.env` *file* for you (from `.env.example`) — you paste the secrets.
- Run `trader propose` and `trader propose --dry-run`, and explain the result.
- Install the approver service (`launchd/install_approver.sh`) — that's *your* software running
  under *your* account; Claude is just running the installer.
- Create the scheduled tasks from the prompts in `cowork/`.
- Add features: a new gate rule with a test, a new indicator, a Robinhood adapter, a Slack
  notifier, a backtester, a dashboard. `docs/TEACHING_NOTES.md` has a list.
- Read the journal and critique the week (the Sunday scheduled task literally does this).

## Things Claude will not do (and why that's the point)

- **Tap "Go ahead" for you**, or otherwise place, approve, or cancel orders — real *or* paper.
  The whole design is that a human presses the button. If you find yourself wanting Claude to
  press it, that's the moment to reread `docs/HOW_IT_WORKS.md`.
- **Type or read your API keys / tokens.** Claude opens the page and stops; you copy and paste.
  Keys go into `.env` (git-ignored) and nowhere else. If a key ever shows up in a terminal or
  a log, revoke it and regenerate.
- **Flip the account to live money.** `ALPACA_PAPER=false` plus the confirmation phrase in
  `config.py` is a deliberate two-handed switch, and both hands are yours.
- **Rewrite `STRATEGY.md`** on its own initiative. It will *suggest* edits (quoted), and you
  decide. It's your thesis.
- **Give investment advice** ("should I buy X?"). It'll analyze the snapshot against your
  strategy, but the strategy is yours to write and the tap is yours to make.

## Where things are, for orientation

| You want to… | Look at |
|---|---|
| change which stocks are considered | `config.toml` → `[universe]` |
| change how much can be risked | `config.toml` → `[risk]` (`capital_cap`, `max_position_pct`, …) |
| change what the brain is told | `STRATEGY.md` |
| understand a rejection you saw on your phone | `src/trader/domain/risk.py` (the reason text is right there) |
| see everything that ever happened | `data/journal.db` (SQLite) — `trader report`, or ask Claude to query it |
| stop everything now | `trader halt` (and `trader resume`) |
| stop the approver service | `./launchd/install_approver.sh uninstall` |
| see what the brain sees | `trader snapshot` |

## A workflow that works

1. **Paper for weeks.** Let the check-ins run. Tap thoughtfully. Read the Sunday review.
2. **Change one thing at a time** — a strategy sentence, a gate limit, a universe symbol —
   and note the date in `STRATEGY.md`, so the journal can be read as an experiment log.
3. **When you add code, add a test.** Claude will do this by default if you ask for "a rule
   and a test"; the gate tests in `tests/test_risk_gate.py` are the pattern to copy.
4. **Commit small, with a message that says why.** Future-you and your siblings will read them.

## If Claude ever seems to be doing the wrong thing

Ask it: *"Which rule in CLAUDE.md applies here?"* Nine times out of ten that resolves it. The
tenth time, you've found either a bug in the code or a gap in the docs — fix the docs, too.
