"""Corporate-events adapter: next earnings date per symbol, via yfinance (Yahoo Finance).

Best-effort by design: a snapshot must never fail because Yahoo hiccuped. Results are cached on
disk for a day (Yahoo is slow — ~0.5s per ticker — and earnings dates don't move hourly).
ETFs have no earnings; they are cached as "none" so we don't ask again.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

_TTL = timedelta(hours=20)


class YFinanceEvents:
    """Implements ``EventsPort``. ``next_earnings`` returns {symbol: ISO date} for names with a known date."""

    def __init__(self, cache_path: Path, max_symbols: int = 25) -> None:
        self._cache_path = cache_path
        self._max = max_symbols

    def next_earnings(self, symbols: list[str]) -> dict[str, str]:
        cache = self._load()
        now = datetime.now(UTC)
        out: dict[str, str] = {}
        todo: list[str] = []
        for s in symbols[: self._max]:
            hit = cache.get(s)
            if hit and datetime.fromisoformat(hit["at"]) > now - _TTL:
                if hit.get("date"):
                    out[s] = hit["date"]
                continue
            todo.append(s)
        if todo:
            try:
                import yfinance as yf  # imported lazily: optional dependency, slow import
            except ImportError:
                log.warning("yfinance not installed; earnings dates unavailable")
                return out
            for s in todo:
                d = _fetch_one(yf, s)
                cache[s] = {"at": now.isoformat(), "date": d}
                if d:
                    out[s] = d
            self._save(cache)
        return out

    def _load(self) -> dict[str, dict[str, str | None]]:
        try:
            if self._cache_path.exists():
                loaded: dict[str, dict[str, str | None]] = json.loads(self._cache_path.read_text())
                return loaded
        except (OSError, ValueError) as exc:
            log.warning("earnings cache unreadable (%s); starting fresh", type(exc).__name__)
        return {}

    def _save(self, cache: dict[str, dict[str, str | None]]) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps(cache, indent=1, sort_keys=True))
        except OSError as exc:
            log.warning("earnings cache not written: %s", type(exc).__name__)


def _fetch_one(yf: object, symbol: str) -> str | None:
    """Next earnings date as ISO string, or None (ETF / unknown / Yahoo error)."""
    try:
        cal = yf.Ticker(symbol).calendar  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 — Yahoo returns 404s for ETFs, rate limits, etc.
        log.debug("earnings lookup failed for %s: %s", symbol, type(exc).__name__)
        return None
    dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
    if not dates:
        return None
    today = date.today()  # noqa: DTZ011 — earnings dates are calendar dates, not instants
    future = sorted(d for d in dates if isinstance(d, date) and d >= today)
    return future[0].isoformat() if future else None
