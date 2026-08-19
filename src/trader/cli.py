"""Command-line entry point: ``trader <command>``.

Commands:
  propose        collect -> decide -> gate -> journal -> notify   (the morning job)
  approve        run the approver loop (your tap -> your Mac submits)  (launchd job)
  snapshot       print the MarketSnapshot as JSON (for a human/agent brain)
  status         account + latest proposal at a glance
  report         daily | weekly report (optionally send to Telegram)
  telegram-test  verify the bot token and discover your chat id
  halt / resume  create / remove the HALT kill-switch file
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from trader.adapters.alpaca_broker import AlpacaBroker
from trader.adapters.claude_decider import ClaudeDecider, FileDecider
from trader.adapters.sqlite_journal import SqliteJournal
from trader.adapters.telegram_notifier import TelegramNotifier
from trader.app.approve import ApproveDeps, run_approver
from trader.app.propose import ProposeDeps, run_propose
from trader.app.report import daily_report, weekly_report
from trader.app.snapshot import take_snapshot
from trader.config import ConfigError, Settings, load_settings
from trader.domain.models import to_json

log = logging.getLogger("trader")

# Libraries that log request URLs. The Telegram Bot API puts the bot token IN the URL,
# so these must never log at INFO — not to the terminal, not to the launchd log, not with -v.
_SECRET_LEAKING_LOGGERS = ("httpx", "httpcore")


def configure_logging(*, verbose: bool = False) -> None:
    """Set up logging so that our own messages are visible and third-party URL logs are not."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for name in _SECRET_LEAKING_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    configure_logging(verbose=args.verbose)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trader", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("propose", help="run the morning proposal cycle")
    sp.add_argument("--dry-run", action="store_true", help="don't send to Telegram; print instead")
    sp.add_argument("--decision", type=Path, help="use a decision.json instead of calling Claude")
    sp.set_defaults(func=cmd_propose)

    sa = sub.add_parser("approve", help="run the approver (long-running)")
    sa.add_argument("--once", action="store_true", help="poll once and exit")
    sa.set_defaults(func=cmd_approve)

    ss = sub.add_parser("snapshot", help="print the market snapshot as JSON")
    ss.add_argument("--out", type=Path, default=None, help="write the JSON to this file instead of stdout")
    ss.set_defaults(func=cmd_snapshot)

    st = sub.add_parser("status", help="account and latest proposal")
    st.set_defaults(func=cmd_status)

    sc = sub.add_parser("chart", help="7-day P&L chart + since-inception/week/month/today numbers")
    sc.add_argument("--days", type=int, default=7, help="chart window in days (default 7)")
    sc.add_argument("--send", action="store_true", help="send the PNG + numbers to Telegram")
    sc.add_argument("--out", type=Path, default=None, help="where to write the PNG (default data/performance.png)")
    sc.set_defaults(func=cmd_chart)

    sr = sub.add_parser("report", help="daily or weekly report")
    sr.add_argument("period", choices=["daily", "weekly"])
    sr.add_argument("--send", action="store_true", help="also send to Telegram")
    sr.set_defaults(func=cmd_report)

    stt = sub.add_parser("telegram-test", help="check bot token, discover chat id, send a hello")
    stt.set_defaults(func=cmd_telegram_test)

    sb = sub.add_parser("brain", help="print which brain is configured: 'api' (ANTHROPIC_API_KEY set) or 'agent'")
    sb.set_defaults(func=cmd_brain)

    sh = sub.add_parser("halt", help="create the HALT file — approver will refuse to submit")
    sh.set_defaults(func=cmd_halt)
    sres = sub.add_parser("resume", help="remove the HALT file")
    sres.set_defaults(func=cmd_resume)
    return p


# ---------------------------------------------------------------------- wiring helpers


def _broker(s: Settings) -> AlpacaBroker:
    return AlpacaBroker(s.alpaca_api_key, s.alpaca_secret_key, paper=s.alpaca_paper)


def _notifier(s: Settings) -> TelegramNotifier:
    return TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id)


def _journal(s: Settings) -> SqliteJournal:
    return SqliteJournal(s.db_path)


def _strategy(s: Settings) -> str:
    if not s.strategy_file.exists():
        raise ConfigError(f"{s.strategy_file.name} not found — write your strategy there first.")
    return s.strategy_file.read_text()


def _banner(s: Settings) -> None:
    if s.is_live:
        log.warning("*** LIVE MODE — real money. ***")
    else:
        log.info("paper mode (fake money)")


# ---------------------------------------------------------------------- commands


def cmd_propose(args: argparse.Namespace) -> int:
    s = load_settings()
    _banner(s)
    broker = _broker(s)
    decider = FileDecider(args.decision) if args.decision else ClaudeDecider(model=s.anthropic_model)
    deps = ProposeDeps(
        broker=broker,
        data=broker,
        decider=decider,
        journal=_journal(s),
        notifier=None if args.dry_run else _notifier(s),
        risk_config=s.risk,
        universe=s.universe,
        history_days=s.history_days,
        mode=s.mode,
        strategy_prompt=_strategy(s),
        halted=s.halt_file.exists(),
    )
    proposal = run_propose(deps)
    from trader.adapters.telegram_notifier import format_proposal  # local import: display only

    print(_strip_html(format_proposal(proposal)))
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    s = load_settings()
    _banner(s)
    broker = _broker(s)
    deps = ApproveDeps(
        broker=broker,
        data=broker,
        journal=_journal(s),
        notifier=_notifier(s),
        risk_config=s.risk,
        universe=s.universe,
        history_days=s.history_days,
        allowed_chat_id=s.telegram_chat_id,
        halted_check=s.halt_file.exists,
        reload_config=lambda: (lambda st: (st.risk, st.universe, st.history_days))(load_settings()),
    )
    run_approver(deps, once=args.once)
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    s = load_settings()
    broker = _broker(s)
    snap = take_snapshot(broker, broker, s.universe, s.history_days, s.risk.capital_cap)
    text = to_json(snap.to_json_dict())
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
        regime = snap.regime.get("verdict")
        print(f"wrote {args.out} ({len(text)} bytes; {len(snap.indicators)} symbols; regime {regime})")
    else:
        print(text)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    s = load_settings()
    broker = _broker(s)
    acct = broker.get_account()
    print(f"mode: {s.mode}   market open: {broker.is_market_open()}   HALT: {s.halt_file.exists()}")
    print(f"equity ${acct.equity:,.2f}  cash ${acct.cash:,.2f}  today {acct.day_pl_pct:+.2%}")
    for p in acct.positions:
        print(
            f"  {p.symbol:6} {p.qty.normalize():>8} @ ${p.avg_entry_price:<9} "
            f"now ${p.current_price:<9} {p.unrealized_plpc:+.2%}"
        )
    latest = _journal(s).latest_pending()
    if latest:
        exp = latest.expires_at.astimezone()
        print(f"pending proposal {latest.id} with {len(latest.accepted)} order(s), expires {exp:%H:%M}")
    else:
        print("no pending proposal")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    s = load_settings()
    broker = _broker(s)
    journal = _journal(s)
    if args.period == "daily":
        text = daily_report(broker, journal, s.mode)
    else:
        text = weekly_report(broker, broker, journal, s.mode, s.benchmark, s.weekly_target_pct)
    print(text)
    if args.send:
        _notifier(s).send_text(text)
    return 0


def cmd_chart(args: argparse.Namespace) -> int:
    from datetime import datetime
    from decimal import Decimal

    from trader.app.chart import build_performance, caption, render_chart
    from trader.domain.models import utcnow

    s = load_settings()
    broker = _broker(s)
    journal = _journal(s)
    # Inception = the moment the book started. Recorded once, in the journal, from the first
    # proposal we ever made; falls back to "now" for a brand-new install.
    inception_raw = journal.get_state("inception_at")
    inception_eq_raw = journal.get_state("inception_equity")
    if not (inception_raw and inception_eq_raw):
        first = journal.list_proposals(datetime(2000, 1, 1, tzinfo=utcnow().tzinfo))
        if first:
            inception = first[0].created_at
            inception_eq = Decimal(str(first[0].snapshot.get("account", {}).get("equity", "0")))
        else:
            inception = utcnow()
            inception_eq = broker.get_account().equity
        journal.set_state("inception_at", inception.isoformat())
        journal.set_state("inception_equity", str(inception_eq))
    else:
        inception = datetime.fromisoformat(inception_raw)
        inception_eq = Decimal(inception_eq_raw)

    # Intraday points for the chart window + daily points for the month-over-month base.
    seen: dict[datetime, Decimal] = {}
    for ts, eq in broker.get_equity_history(31) + broker.get_equity_history(min(args.days, 7)):
        seen[ts] = eq
    # Every check-in journals the equity it saw — free intraday points that fill the line in.
    for p in journal.list_proposals(utcnow() - __import__("datetime").timedelta(days=args.days + 1)):
        eq_raw = p.snapshot.get("account", {}).get("equity")
        if eq_raw:
            seen[p.created_at] = Decimal(str(eq_raw))
    acct_now = broker.get_account()
    seen[utcnow()] = acct_now.equity  # the broker's history lags; the live number is truth
    history = sorted(seen.items())
    spy_now = (broker.get_quotes([s.benchmark]).get(s.benchmark) or None)
    spy_now_price = spy_now.price if spy_now else None
    spy_bars = broker.get_daily_bars([s.benchmark], max(args.days, 31) + 5).get(s.benchmark, [])
    fills = broker.get_orders_since(utcnow() - __import__("datetime").timedelta(days=args.days + 1))
    perf = build_performance(
        history, inception=inception, inception_equity=inception_eq, capital_cap=s.risk.capital_cap or inception_eq,
        spy_bars=spy_bars, fills=fills, window_days=args.days, spy_now=spy_now_price,
        prev_close_equity=acct_now.last_equity,
    )
    out = args.out or (s.project_root / "data" / "performance.png")
    render_chart(perf, out, title=f"Book P&L — last {args.days} days [{s.mode.upper()}]")
    text = caption(perf, s.mode)
    print(text)
    print(f"chart: {out}")
    if args.send:
        _notifier(s).send_photo(str(out), caption=text)
        print("sent to telegram")
    return 0


def cmd_telegram_test(args: argparse.Namespace) -> int:
    s = load_settings(require_broker=False)
    if not s.telegram_bot_token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is empty.")
    tn = TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id or "0")
    me = tn.whoami()
    print(f"bot: @{me.get('username')} ({me.get('first_name')})")
    if not s.telegram_chat_id:
        chats = tn.recent_chat_ids()
        if not chats:
            print("No chats yet. Open Telegram, message your bot once (say 'hi'), then rerun.")
        else:
            print("Recent chats — put the right one in .env as TELEGRAM_CHAT_ID:")
            for cid, name in chats:
                print(f"  {cid}   ({name})")
        return 0
    tn.send_text("👋 trading-agent can reach you. Approvals will arrive here.")
    print(f"sent a hello to chat {s.telegram_chat_id}")
    return 0


def cmd_brain(args: argparse.Namespace) -> int:
    """Lets scheduled tasks pick the decision path without ever reading .env themselves."""
    import os

    load_settings(require_broker=False)  # loads .env into the process environment
    print("api" if os.environ.get("ANTHROPIC_API_KEY", "").strip() else "agent")
    return 0


def cmd_halt(args: argparse.Namespace) -> int:
    s = load_settings(require_broker=False)
    s.halt_file.write_text("halted\n")
    print(f"created {s.halt_file} — the approver will refuse to submit until you run `trader resume`.")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    s = load_settings(require_broker=False)
    if s.halt_file.exists():
        s.halt_file.unlink()
        print("HALT removed.")
    else:
        print("not halted.")
    return 0


def _strip_html(s: str) -> str:
    import re

    return re.sub(r"<[^>]+>", "", s).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


if __name__ == "__main__":
    sys.exit(main())
