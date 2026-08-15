# Setup — from zero to a proposal on your phone

Budget about 20 minutes. You need: a Mac (for the launchd part; the rest runs anywhere),
Python 3.11+, a Telegram account, and an Alpaca account. Everything below is on a **paper**
account. Do the live-money step last, or never.

## 1. The code

```bash
git clone <this repo> ~/Development/trading-agent
cd ~/Development/trading-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest        # 46 tests, all offline, ~1 s
```

If the tests pass, the code is fine. Everything from here is credentials and wiring.

## 2. Alpaca (paper trading)

1. Sign up at <https://alpaca.markets>. Choose the free plan.
2. In the dashboard, switch to **Paper Trading** (top-left toggle).
3. **API Keys → Generate**. Copy the key and secret somewhere safe *now* — the secret is shown once.
4. Paper accounts come pre-funded with fake money (often $100k). If you want it to feel like a
   $1,000 account, the paper dashboard lets you **reset** the account with a custom balance.

## 3. Telegram bot

1. In Telegram, message **@BotFather** → `/newbot` → pick a name and a username ending in `bot`.
2. Copy the token it gives you (looks like `123456789:AAH...`).
3. Open a chat with your new bot and send it any message ("hi") — bots can't message you first.

## 4. `.env`

```bash
cp .env.example .env
```

Fill in `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `TELEGRAM_BOT_TOKEN`. Leave `ALPACA_PAPER=true`.
Leave `TRADER_LIVE_CONFIRM` empty. Then:

```bash
trader telegram-test
```

It prints your bot's name and lists recent chats with their ids. Put yours in `.env` as
`TELEGRAM_CHAT_ID`, run `trader telegram-test` again, and you should get a 👋 on your phone.

## 5. The brain

Two options — pick one:

- **API brain (recommended for portability).** Create a key at
  <https://platform.claude.com>, put it in `.env` as `ANTHROPIC_API_KEY`. `trader propose`
  will call Claude directly. Cost per morning is a few cents.
- **Agent brain.** Leave the key empty and let a Claude Desktop scheduled task be the brain
  (`cowork/morning-proposal.md`, "Alternative" section). No API key, but it only works where
  Claude Desktop runs.

## 6. Your strategy

Open `STRATEGY.md`. It ships with a sensible template. Read it, change it to say what *you*
actually believe. Then look at `config.toml` and decide whether the risk limits suit your
account size — for a $1,000 account the defaults ($400 max order, 25% max position, 10% cash
buffer, 3% daily-loss breaker) are reasonable.

## 7. First cycle

```bash
trader status              # should show your paper equity, no positions
trader propose --dry-run   # full cycle, prints the proposal, sends nothing
trader propose             # sends it. Look at your phone.
```

Don't tap yet — the approver isn't running, so nothing would happen anyway.

## 8. The approver (your Mac)

```bash
./launchd/install_approver.sh
tail -f data/approver.log
```

Now tap **Go ahead** on the Telegram message. Within a few seconds the buttons disappear,
"✅ Approved" appears, and you get a confirmation with Alpaca order ids. Check
`trader status` — you'll see the position (paper accounts fill limit orders when the market is
open; outside hours the order queues for the open).

To stop the approver: `./launchd/install_approver.sh uninstall`. Kill switch without stopping
anything: `trader halt` (and `trader resume`).

## 9. Schedule it (Claude Desktop / Cowork)

Create three scheduled tasks using the prompts in `cowork/`. From a Claude Desktop chat you
can literally say: *"Create a scheduled task called trading-morning-proposal that runs at 8:30
on weekdays with this prompt: …"* and paste the file. Or use plain cron:

```
30 8 * * 1-5  cd ~/Development/trading-agent && .venv/bin/trader propose >> data/propose.log 2>&1
15 16 * * 1-5 cd ~/Development/trading-agent && .venv/bin/trader report daily --send >> data/report.log 2>&1
0 18 * * 0    cd ~/Development/trading-agent && .venv/bin/trader report weekly --send >> data/report.log 2>&1
```

Remember: scheduled tasks only fire when the machine is awake (and, for Cowork, when the app
is open). If you want 8:30 sharp, set the Mac to wake at 8:25 on weekdays.

## 10. Live money (read twice, do once, or never)

Only after weeks of paper trading where the journal shows the process is sound:

1. In `.env`: `ALPACA_PAPER=false` and `TRADER_LIVE_CONFIRM=I_UNDERSTAND_THIS_IS_REAL_MONEY`.
2. Generate **live** API keys in Alpaca (they're different from paper keys) and put them in `.env`.
3. `trader status` — you should see the real balance and the log line `*** LIVE MODE ***`.
4. Reinstall the approver so it picks up the new env: `./launchd/install_approver.sh`.
5. Consider halving every limit in `config.toml` for the first month.

Telegram messages show `🔴 LIVE` in the header when real money is in play.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `config error: ALPACA_API_KEY ... missing` | `.env` not filled, or you're not in the project folder |
| `telegram getUpdates ... 409 Conflict` | two approvers are polling the same bot — stop one |
| Proposal arrives, tap does nothing | approver not running: `launchctl print gui/$(id -u)/com.trading-agent.approver`, check `data/approver.log` |
| `subscription does not permit querying recent SIP data` | data feed set to SIP; the adapter uses IEX by default — check you didn't change it |
| Every order rejected "not in the configured universe" | symbol case or missing from `config.toml [universe]` |
| No quotes / empty snapshot on weekends | normal — IEX snapshot may be sparse when closed; the daily bars still work |
