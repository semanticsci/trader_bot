# Setup — from zero to a proposal on your phone

Budget about 20 minutes. You need: a Mac (for the launchd part; the rest runs anywhere),
Python 3.11+, a Telegram account, and an Alpaca account. Everything below is on a **paper**
account. Do the live-money step last, or never.

## 1. The code

```bash
git clone https://github.com/semanticsci/trader_bot.git ~/Development/trading-agent
cd ~/Development/trading-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest        # 51 tests, all offline, ~1 s
```

If the tests pass, the code is fine. Everything from here is credentials and wiring.

## 2. Alpaca (paper trading)

1. Sign up at <https://alpaca.markets>. Choose the free plan.
2. In the dashboard, switch to **Paper Trading** (top-left toggle).
3. **API Keys → Generate**. Copy the key and secret somewhere safe *now* — the secret is shown once.
4. Paper accounts come pre-funded with fake money (usually $100k). You don't need to reset it:
   `config.toml` has `capital_cap = 1000`, and the risk gate measures every size rule against
   that budget, so the $100k behaves like $1,000. Change the cap to change the budget.

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
  will call Claude directly. Cost per check-in is a few cents.
- **Agent brain.** Leave the key empty and let a Claude Desktop scheduled task be the brain
  (`cowork/proposal.md`, "Alternative" section). No API key, but it only works where
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
can literally say: *"Create a scheduled task called trading-proposal-open that runs at 09:45
on weekdays with this prompt: …"* and paste `cowork/proposal.md`; repeat for 12:30 and 15:00,
then the EOD summary and the Sunday review. Or use plain cron:

```
45 9  * * 1-5 cd ~/Development/trading-agent && .venv/bin/trader propose >> data/propose.log 2>&1
30 12 * * 1-5 cd ~/Development/trading-agent && .venv/bin/trader propose >> data/propose.log 2>&1
0  15 * * 1-5 cd ~/Development/trading-agent && .venv/bin/trader propose >> data/propose.log 2>&1
15 16 * * 1-5 cd ~/Development/trading-agent && .venv/bin/trader report daily --send >> data/report.log 2>&1
0  18 * * 0   cd ~/Development/trading-agent && .venv/bin/trader report weekly --send >> data/report.log 2>&1
```
(cron needs `ANTHROPIC_API_KEY` in `.env`, since there is no Cowork agent to be the brain.)

### Keep the Mac awake, or none of this runs on time

Scheduled tasks only fire when the machine is awake (and, for Cowork, when the app is open).
This is not a footnote — it is the single most common reason the pipeline "does nothing". A Mac
set to idle-sleep after a minute will run the 12:38 check-in whenever you next open the lid.
We measured exactly that: on Aug 19–20 the midday run landed at 14:24 and the pre-close run at
19:44, almost four hours after the closing bell, proposing into a market that had shut.

Install the wake agent (weekdays, 09:25 → 16:45 New York, no password needed):

```
cp launchd/com.trading-agent.markethours-awake.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.trading-agent.markethours-awake.plist
```

It runs `caffeinate -i`, which blocks *idle* sleep while letting the display sleep normally, so
the battery cost is modest. Two things it cannot do: it will not beat **closing the lid**
(clamshell sleep), and it cannot wake a Mac that is already fully asleep at 09:25. Leave the lid
open. If you want the Mac to wake itself before the open, that needs one `sudo` command:

```
sudo pmset repeat wakeorpoweron MTWRF 09:20:00
```

To remove the agent: `launchctl bootout gui/$(id -u)/com.trading-agent.markethours-awake` and
delete the plist.

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
