"""Small pure-function tests: indicators, Telegram parsing, decision parsing, journal, config."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trader.adapters.claude_decider import parse_decision
from trader.adapters.sqlite_journal import SqliteJournal
from trader.adapters.telegram_notifier import classify_text, format_proposal, parse_update
from trader.config import ConfigError, load_settings
from trader.domain.indicators import compute_indicators, pct_change, sma
from trader.domain.models import Bar, Proposal, ProposalStatus, ProposedOrder, Side, utcnow

# ---------------------------------------------------------------- indicators


def _bars(closes: list[int]) -> list[Bar]:
    return [Bar(f"2026-01-{i + 1:02d}", Decimal(c), Decimal(c + 1), Decimal(c - 1), Decimal(c), 100) for i, c in enumerate(closes)]


def test_sma_needs_enough_data():
    assert sma([Decimal(1), Decimal(2)], 3) is None
    assert sma([Decimal(1), Decimal(2), Decimal(3)], 3) == Decimal("2.00")


def test_pct_change():
    assert pct_change(Decimal("110"), Decimal("100")) == Decimal("0.1000")
    assert pct_change(Decimal("1"), Decimal("0")) is None


def test_compute_indicators_partial_history():
    ind = compute_indicators(_bars(list(range(100, 110))))  # 10 bars only
    assert ind is not None
    assert ind.last_close == Decimal("109")
    assert ind.sma_20 is None and ind.sma_50 is None
    assert ind.return_5d_pct == pct_change(Decimal(109), Decimal(104))


def test_compute_indicators_full():
    ind = compute_indicators(_bars(list(range(100, 160))))  # 60 bars
    assert ind.sma_20 == Decimal("149.50")
    assert ind.high_20d == Decimal("160")  # high = close+1 of last bar (159+1)
    assert ind.avg_volume_20d == 100
    assert compute_indicators([]) is None


# ---------------------------------------------------------------- telegram parsing


def test_parse_callback_approve():
    upd = {"update_id": 7, "callback_query": {"id": "cq", "data": "approve:abc123", "message": {"chat": {"id": 42}, "message_id": 9}}}
    tap = parse_update(upd)
    assert tap.kind == "approve" and tap.proposal_id == "abc123" and tap.chat_id == "42" and tap.update_id == 7


def test_parse_text_message():
    tap = parse_update({"update_id": 8, "message": {"chat": {"id": 42}, "text": " go ", "message_id": 3}})
    assert tap.kind == "text" and tap.text == "go"


def test_parse_ignores_unknown():
    assert parse_update({"update_id": 9, "edited_message": {}}) is None
    assert parse_update({"update_id": 10, "callback_query": {"data": "weird"}}) is None


@pytest.mark.parametrize("text,expected", [("go", "approve"), ("YES!", "approve"), ("no", "skip"), ("what?", None), ("go ahead", "approve")])
def test_classify_text(text, expected):
    assert classify_text(text) == expected


def test_format_proposal_mentions_orders_and_id():
    p = Proposal(
        id="abc123", created_at=utcnow(), expires_at=utcnow() + timedelta(hours=1), mode="paper",
        accepted=(ProposedOrder("NVDA", Side.BUY, Decimal("1"), Decimal("180.5"), "why <b>"),),
        rejected=(), summary="Calm day & up.",
    )
    text = format_proposal(p)
    assert "BUY 1 NVDA" in text and "id:abc123" in text
    assert "&lt;b&gt;" in text and "&amp;" in text  # HTML-escaped user content


# ---------------------------------------------------------------- decision parsing


def test_parse_decision_ok():
    d = parse_decision({"summary": "s", "orders": [{"symbol": "nvda", "side": "buy", "qty": "1.5", "limit_price": "180.10", "rationale": "r"}]})
    assert d.orders[0].symbol == "NVDA" and d.orders[0].qty == Decimal("1.5")


def test_parse_decision_rejects_garbage():
    with pytest.raises(ValueError):
        parse_decision({"summary": "s", "orders": [{"symbol": "X", "side": "hold", "qty": "1", "limit_price": "1", "rationale": ""}]})
    with pytest.raises(ValueError):
        parse_decision({"summary": "s", "orders": [{"symbol": "X", "side": "buy", "qty": "one", "limit_price": "1", "rationale": ""}]})


# ---------------------------------------------------------------- journal


def test_journal_roundtrip_and_state(tmp_path: Path):
    j = SqliteJournal(tmp_path / "j.db")
    p = Proposal(
        id="p1", created_at=utcnow(), expires_at=utcnow() + timedelta(hours=1), mode="paper",
        accepted=(ProposedOrder("NVDA", Side.BUY, Decimal("1"), Decimal("180"), "r"),),
        rejected=(), summary="s", snapshot={"account": {"equity": "1000"}},
    )
    j.save_proposal(p)
    assert j.latest_pending().id == "p1"
    p.status = ProposalStatus.SKIPPED
    j.save_proposal(p)
    assert j.latest_pending() is None
    assert j.get_proposal("p1").accepted[0].limit_price == Decimal("180")
    assert j.get_state("x") is None
    j.set_state("x", "1")
    j.set_state("x", "2")
    assert j.get_state("x") == "2"
    j.log_event("test", {"a": Decimal("1.5")})
    assert j.list_events(utcnow() - timedelta(minutes=1))[0][1] == "test"


# ---------------------------------------------------------------- config safety


def test_live_requires_confirmation_phrase(tmp_path: Path, monkeypatch):
    (tmp_path / "config.toml").write_text("[risk]\n[universe]\nsymbols=['SPY']\n")
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("ALPACA_PAPER", "false")
    monkeypatch.delenv("TRADER_LIVE_CONFIRM", raising=False)
    with pytest.raises(ConfigError):
        load_settings(tmp_path)
    monkeypatch.setenv("TRADER_LIVE_CONFIRM", "I_UNDERSTAND_THIS_IS_REAL_MONEY")
    s = load_settings(tmp_path)
    assert s.is_live and s.universe == ("SPY",)


def test_paper_is_default(tmp_path: Path, monkeypatch):
    (tmp_path / "config.toml").write_text("[risk]\nmax_order_notional=250\n")
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.delenv("ALPACA_PAPER", raising=False)
    s = load_settings(tmp_path)
    assert s.mode == "paper" and s.risk.max_order_notional == Decimal("250")


# ---------------------------------------------------------------- secrets never reach logs


def test_httpx_url_logging_is_silenced_even_when_verbose():
    """The Telegram Bot API puts the token in the URL; httpx logs URLs at INFO. We must silence it."""
    import logging

    from trader.cli import configure_logging

    configure_logging(verbose=True)
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpcore").isEnabledFor(logging.INFO)


# ---------------------------------------------------------------- telegram transport retries


def test_telegram_call_retries_transient_errors(monkeypatch):
    """A connection reset on the first try must not lose the message."""
    import httpx

    from trader.adapters.telegram_notifier import TelegramNotifier

    tn = TelegramNotifier("t", "1", max_attempts=3)
    calls = {"n": 0}

    class Resp:
        def json(self):
            return {"ok": True, "result": {"message_id": 42}}

    def flaky_post(url, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("Connection reset by peer")
        return Resp()

    monkeypatch.setattr(tn._http, "post", flaky_post)
    monkeypatch.setattr("trader.adapters.telegram_notifier.time.sleep", lambda s: None)
    assert tn._call("sendMessage", {})["message_id"] == 42
    assert calls["n"] == 2


def test_telegram_call_does_not_retry_api_errors(monkeypatch):
    """A 'chat not found' from Telegram is not transient — fail fast, once."""
    import httpx
    import pytest

    from trader.adapters.telegram_notifier import TelegramNotifier

    tn = TelegramNotifier("t", "1", max_attempts=3)
    calls = {"n": 0}

    class Resp:
        def json(self):
            return {"ok": False, "description": "Bad Request: chat not found"}

    def post(url, json=None, timeout=None):
        calls["n"] += 1
        return Resp()

    monkeypatch.setattr(tn._http, "post", post)
    with pytest.raises(httpx.HTTPError):
        tn._call("sendMessage", {})
    assert calls["n"] == 1


# ---------------------------------------------------------------- performance chart numbers


def test_build_performance_numbers_and_series():
    from datetime import UTC, datetime, timedelta

    from trader.app.chart import build_performance, caption
    from trader.domain.models import Bar, BrokerOrder, Side

    inception = datetime(2026, 8, 15, 20, 0, tzinfo=UTC)
    now = inception + timedelta(days=2, hours=4)
    hist = [
        (inception - timedelta(days=1), Decimal("100000")),  # before inception: ignored in the series
        (inception + timedelta(days=2), Decimal("100010")),
        (now, Decimal("100025")),
    ]
    bars = [Bar("2026-08-14", Decimal(1), Decimal(1), Decimal(1), Decimal("770"), 1),
            Bar("2026-08-17", Decimal(1), Decimal(1), Decimal(1), Decimal("777.7"), 1)]  # +1%
    fills = [BrokerOrder("o1", "AMD", Side.BUY, Decimal("0.47"), Decimal("511.82"), "filled",
                         filled_qty=Decimal("0.47"), filled_avg_price=Decimal("511.66"),
                         filled_at=inception + timedelta(days=1, hours=17))]
    p = build_performance(hist, inception=inception, inception_equity=Decimal("100000"), capital_cap=Decimal("1000"),
                          spy_bars=bars, fills=fills, now=now, window_days=7)
    assert p.since_inception == Decimal("25") and p.pct(p.since_inception) == Decimal("2.5")
    assert p.week == Decimal("25") and p.month == Decimal("25")  # book younger than a week/month → from inception
    assert p.pnl_series[0] == (inception, Decimal("0")) and p.pnl_series[-1][1] == Decimal("25")
    assert p.spy_since_inception == Decimal("10")  # 1% of $1,000, based on the last close on/before inception
    assert p.spy_series[0] == (inception, Decimal("0"))
    assert len(p.fills) == 1 and p.fills[0][1] == "AMD"
    text = caption(p, "paper")
    assert "+$25.00" in text and "+2.50%" in text and "SPY" in text


def test_strip_base_value_offset_fixes_alpaca_intraday_equity():
    """Alpaca's 15Min portfolio history reports equity one whole base_value too high once the
    account has traded; the flat pre-trade points are already correct. Fix only the wrong ones."""
    from datetime import UTC, datetime

    from trader.adapters.alpaca_broker import strip_base_value_offset

    t = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)
    points = [
        (t, Decimal("100000.00")),     # before the first trade: correct as-is
        (t, Decimal("199999.88")),     # base_value double-counted
        (t, Decimal("199965.59")),     # base_value double-counted
    ]
    fixed = strip_base_value_offset(points, base_value=Decimal("100000"), anchor=Decimal("99965.28"))
    assert [eq for _, eq in fixed] == [Decimal("100000.00"), Decimal("99999.88"), Decimal("99965.59")]


def test_strip_base_value_offset_leaves_a_healthy_series_alone():
    from datetime import UTC, datetime

    from trader.adapters.alpaca_broker import strip_base_value_offset

    t = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)
    points = [(t, Decimal("100000")), (t, Decimal("99971.11")), (t, Decimal("99965.28"))]
    assert strip_base_value_offset(points, base_value=Decimal("100000"), anchor=Decimal("99965.28")) == points
    # no base_value reported → nothing to undo
    assert strip_base_value_offset(points, base_value=Decimal("0"), anchor=Decimal("99965.28")) == points


def test_build_performance_handles_no_history():
    from datetime import UTC, datetime

    from trader.app.chart import build_performance

    inception = datetime(2026, 8, 15, tzinfo=UTC)
    p = build_performance([], inception=inception, inception_equity=Decimal("1000"), capital_cap=Decimal("1000"),
                          spy_bars=[], fills=[], now=inception)
    assert p.since_inception == 0 and "no equity history" in " ".join(p.warnings)


# ---------------------------------------------------------------- wide-view indicators


def _ohlc(closes, spread=Decimal("2")):
    from trader.domain.models import Bar
    return [Bar(f"2026-0{1 + i // 28}-{(i % 28) + 1:02d}", Decimal(c) - 1, Decimal(c) + spread, Decimal(c) - spread, Decimal(c), 1000 + i)
            for i, c in enumerate(closes)]


def test_rsi_extremes_and_midpoint():
    from trader.domain.indicators import rsi
    up = [Decimal(100 + i) for i in range(20)]
    down = [Decimal(120 - i) for i in range(20)]
    assert rsi(up) == Decimal("100")
    assert rsi(down) == Decimal("0.0")
    assert rsi([Decimal(100)] * 10) is None  # not enough data


def test_atr_pct_reflects_range():
    from trader.domain.indicators import atr_pct
    bars = _ohlc([100] * 20, spread=Decimal("2"))  # high-low = 4 every day on a $100 stock
    assert atr_pct(bars) == Decimal("0.0400")
    assert atr_pct(bars[:5]) is None


def test_compute_indicators_wide_fields():
    from trader.domain.indicators import compute_indicators
    from trader.domain.models import Quote
    bars = _ohlc(list(range(100, 160)))  # steady uptrend, last close 159
    q = Quote("X", Decimal("160"), Decimal("159"), Decimal("0.0063"))
    ind = compute_indicators(bars, quote=q, spy_return_20d=Decimal("0.05"))
    assert ind.rsi_14 == Decimal("100")  # straight up
    assert ind.atr_pct_14 is not None and ind.rs_20d_vs_spy is not None
    assert ind.rs_20d_vs_spy == ind.return_20d_pct - Decimal("0.05")
    assert ind.gap_pct is not None and ind.range_pos is not None and ind.volume_ratio is not None
    assert ind.dist_to_high_20d_pct is not None


def test_rank_and_regime():
    from trader.domain.indicators import compute_indicators, market_regime, rank_universe
    strong = compute_indicators(_ohlc(list(range(100, 160))), spy_return_20d=Decimal("0.02"))
    weak = compute_indicators(_ohlc(list(range(160, 100, -1))), spy_return_20d=Decimal("0.02"))
    spy = compute_indicators(_ohlc([100 + i // 3 for i in range(60)]))
    inds = {"STRONG": strong, "WEAK": weak, "SPY": spy}
    ranking = rank_universe(inds, top=1, bottom=1)
    assert ranking[0]["symbol"] == "STRONG" and ranking[0]["tag"] == "leader"
    assert ranking[-1]["symbol"] == "WEAK" and ranking[-1]["tag"] == "laggard"
    regime = market_regime(inds, "SPY")
    assert regime["verdict"] in {"risk_on", "neutral", "risk_off"} and regime["breadth_above_sma20"] is not None


# --------------------------------------------------------------------------- new data sources


def test_yfinance_events_uses_cache_and_never_raises(tmp_path, monkeypatch) -> None:
    """Arrange a warm cache: the adapter must answer from it without touching yfinance."""
    import json
    from datetime import UTC, datetime

    from trader.adapters.yfinance_events import YFinanceEvents

    cache = tmp_path / "earnings.json"
    cache.write_text(json.dumps({
        "NVDA": {"at": datetime.now(UTC).isoformat(), "date": "2026-08-26"},
        "SPY": {"at": datetime.now(UTC).isoformat(), "date": None},  # ETF: known to have none
    }))
    # Act — if it tried the network we'd know: yfinance is made un-importable.
    monkeypatch.setitem(__import__("sys").modules, "yfinance", None)
    out = YFinanceEvents(cache).next_earnings(["NVDA", "SPY"])
    # Assert
    assert out == {"NVDA": "2026-08-26"}


def test_snapshot_carries_book_events_and_sectors(broker) -> None:
    """The brain sees its own history, earnings dates and sector tags."""
    from datetime import UTC, datetime
    from decimal import Decimal

    from trader.app.snapshot import take_snapshot

    class Events:
        def next_earnings(self, symbols):  # noqa: ANN001, ANN202
            return {"AAPL": "2026-08-21"}

    snap = take_snapshot(
        broker, broker, ("AAPL", "NVDA", "SPY"), 60, Decimal("1000"),
        with_news=False, events=Events(),
        inception=(datetime(2026, 8, 15, tzinfo=UTC), Decimal("990")),
        last_decision={"status": "skipped", "summary": "x"},
    )
    assert snap.events["AAPL"]["next_earnings"] == "2026-08-21"
    assert snap.events["AAPL"]["held"] is True
    assert snap.book["holdings"][0] == {
        "symbol": "AAPL", "qty": "2", "market_value": "400", "unrealized_pl_pct": "0.00%", "sector": "tech",
    }
    assert snap.book["sector_exposure"] == {"tech": "40.0%"}
    assert snap.book["since_inception"]["pnl_dollars"] == "10.00"
    assert snap.book["last_decision"]["status"] == "skipped"
    assert all("sector" in r for r in snap.ranking)


def test_sector_of_unknown_is_other() -> None:
    from trader.domain.sectors import sector_of

    assert sector_of("nvda") == "semis"
    assert sector_of("ZZZZ") == "other"
