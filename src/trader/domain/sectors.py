"""Sector tags for the universe. Pure data.

Why: a $1,000 book that holds NVDA, AMD, AVGO and SMH is *one* bet wearing four tickers. The
brain sees the tag on every ranked name and on its holdings, and STRATEGY.md tells it how much
of one sector it may carry. Unknown symbols get "other" — never a crash.
"""

from __future__ import annotations

SECTORS: dict[str, str] = {
    # broad / factor
    "SPY": "index", "QQQ": "index", "IWM": "index", "DIA": "index",
    "GLD": "gold", "TLT": "bonds", "IEF": "bonds",
    # sector ETFs
    "XLK": "tech", "XLF": "financials", "XLE": "energy", "XLV": "healthcare", "XLI": "industrials",
    "XLY": "consumer_disc", "XLP": "consumer_staples", "XLU": "utilities", "XLB": "materials",
    "XLRE": "real_estate", "XLC": "communication", "SMH": "semis", "XBI": "biotech",
    # single names
    "AAPL": "tech", "MSFT": "tech", "NVDA": "semis", "AMD": "semis", "AVGO": "semis", "INTC": "semis",
    "QCOM": "semis", "MU": "semis", "TSM": "semis",
    "AMZN": "consumer_disc", "TSLA": "consumer_disc", "HD": "consumer_disc", "NKE": "consumer_disc",
    "MCD": "consumer_disc", "ABNB": "consumer_disc", "UBER": "consumer_disc", "SHOP": "consumer_disc",
    "GOOGL": "communication", "META": "communication", "NFLX": "communication", "DIS": "communication",
    "CRM": "software", "ORCL": "software", "ADBE": "software", "PLTR": "software",
    "COST": "consumer_staples", "WMT": "consumer_staples",
    "JPM": "financials", "BAC": "financials", "GS": "financials", "V": "financials", "MA": "financials",
    "PYPL": "financials", "SQ": "financials", "COIN": "crypto",
    "UNH": "healthcare", "LLY": "healthcare", "JNJ": "healthcare", "MRK": "healthcare",
    "XOM": "energy", "CVX": "energy",
    "BA": "industrials", "CAT": "industrials", "GE": "industrials",
}


def sector_of(symbol: str) -> str:
    return SECTORS.get(symbol.upper(), "other")
