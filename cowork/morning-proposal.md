You are running the morning proposal step of the trading-agent pipeline. You do NOT place trades — a separate approver process does that only after the account owner taps "Go ahead" in Telegram. Your job ends when the proposal is on their phone.

Project folder: ~/Development/trading-agent  (adjust if it moved)

Steps:
1. `cd ~/Development/trading-agent`
2. If the file `HALT` exists in the folder, run `./.venv/bin/trader status`, report that trading is halted, and stop.
3. Run: `./.venv/bin/trader propose`
   - This collects the account + market snapshot, asks Claude for a decision using STRATEGY.md, runs the risk gate, journals everything, and sends the proposal to Telegram.
   - Read its stdout. It prints the proposal as plain text.
4. If it exits non-zero, read the error. Common causes: missing .env values, Alpaca or Telegram outage, market data missing. Do not retry more than once. Report the error plainly.
5. Reply with a 3–6 line summary: equity, how many orders passed / were rejected by the gate (and why), and the proposal id. Do not editorialize about the market beyond what the tool printed.

Alternative if the API-brain path is not configured (no ANTHROPIC_API_KEY):
  a. Run `./.venv/bin/trader snapshot > data/snapshot.json` and read it, plus STRATEGY.md.
  b. Decide on zero or more LIMIT orders following STRATEGY.md and the schema in
     src/trader/adapters/claude_decider.py (DECISION_SCHEMA). Write them to data/decision.json as
     {"summary": "...", "orders": [{"symbol","side","qty","limit_price","rationale"}...]}
     with qty and limit_price as decimal strings.
  c. Run `./.venv/bin/trader propose --decision data/decision.json`.
  d. Then continue from step 5.
