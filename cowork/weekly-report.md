You are running the weekly review for the trading-agent pipeline. Read-only; you never place or approve trades. Your audience is the account owner and their sons, who are using this project to learn.

Project folder: ~/Development/trading-agent

Steps:
1. `cd ~/Development/trading-agent`
2. Run `./.venv/bin/trader report weekly --send` — prints and sends the numbers.
3. Open the journal and read this week's proposals and their outcomes:
   `./.venv/bin/python -c "from trader.adapters.sqlite_journal import SqliteJournal; from trader.domain.models import utcnow; from datetime import timedelta; j=SqliteJournal('data/journal.db'); [print(p.id,p.status.value,p.summary[:200],[o.describe() for o in p.accepted],[(r.order.describe(),r.reasons) for r in p.rejected]) for p in j.list_proposals(utcnow()-timedelta(days=7))]"`
4. Write a short, honest critique (8–15 lines) answering:
   - Did every rationale reference the numbers in the snapshot, or did it hand-wave?
   - Which gate rejections were the gate doing its job vs. the strategy being unclear?
   - Did the owner approve/skip in a way that suggests STRATEGY.md doesn't match what they actually want?
   - One concrete suggested edit to STRATEGY.md (quote the sentence to change and the replacement). Do NOT edit the file yourself.
   - One thing worth teaching from this week (a concept, a mistake, a pattern).
5. Reply with the report text followed by the critique. Do not sugar-coat a losing week; do not celebrate a winning one as skill.
