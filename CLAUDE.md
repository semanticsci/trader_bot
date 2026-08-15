# trading-agent — instructions for Claude Code

This is an **educational, human-approved LLM trading pipeline**. Read `README.md` and
`docs/HOW_IT_WORKS.md` before changing anything. The people working on it are learning; prefer
clear over clever, and explain non-obvious changes in commit messages.

## Non-negotiable rules

1. **The LLM never places orders.** The only call to `broker.submit_limit_order` is in
   `src/trader/app/approve.py::submit_proposal`, and it runs only after a human tap. Do not add
   a second call site. Do not add "auto-approve" flags.
2. **Every proposed order goes through `domain/risk.py`.** New order paths must call
   `risk.evaluate`. New rules need a test in `tests/test_risk_gate.py` and a plain-English reason.
3. **Paper by default.** Never weaken `config.py`'s live-money interlock (`LIVE_CONFIRM_PHRASE`).
4. **No secrets in code, logs, or commits.** Only `config.py` reads environment variables.
   `.env` is git-ignored; keep it that way. Never print API keys.
5. **Money is `Decimal`; datetimes are timezone-aware UTC.** No `float` for prices/quantities.
6. **Domain stays pure.** `src/trader/domain/` imports nothing outside the standard library
   and itself. Adapters implement `ports.py`; the app layer takes dependencies as arguments.
7. **Tests must pass offline.** `python -m pytest` uses `FakeBroker` and in-memory SQLite.
   Don't add tests that need network or real keys.

## Layout

```
src/trader/domain/    models, indicators, risk gate (pure)
src/trader/ports.py   Protocols: BrokerPort, MarketDataPort, DeciderPort, NotifierPort, JournalPort
src/trader/adapters/  alpaca_broker, claude_decider, telegram_notifier, sqlite_journal, fake_broker
src/trader/app/       snapshot, propose, approve, report
src/trader/cli.py     `trader` entry point
tests/                pytest, offline
cowork/               scheduled-task prompts (Claude Desktop)
launchd/              approver as a macOS service
docs/                 HOW_IT_WORKS, SETUP, TEACHING_NOTES
STRATEGY.md           the owner's strategy prompt (theirs to edit; don't rewrite it unasked)
config.toml           universe + risk limits (enforced in code)
```

## Working here

- Activate the venv first: `source .venv/bin/activate` (create with `python3 -m venv .venv && pip install -e ".[dev]"`).
- Run tests: `python -m pytest -q`. Lint: `ruff check src tests`.
- Try the pipeline without sending anything: `trader propose --dry-run`.
- When adding a broker/notifier/decider, add it under `adapters/`, implement the port, wire it in
  `cli.py`, and add a fake or scripted version for tests if it isn't trivially mockable.
- Keep functions small and named for what they do; this repo is meant to be read.
- Adding a dependency? Add it to `pyproject.toml`, and to `.gitignore` if it creates artifacts.

## Things Claude Code should not do here

- Approve, submit, or cancel real or paper orders on the user's behalf. Building and testing the
  code is fine; pressing "Go ahead" is the human's job (that's the whole point of the design).
- Edit `STRATEGY.md` content without being asked — suggest changes, quote them, let the owner decide.
- Loosen risk limits in `config.toml` to "make a test pass."
