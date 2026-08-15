"""Load settings from ``.env`` (secrets) and ``config.toml`` (everything else).

There is exactly one place secrets are read: here. Nothing else touches
``os.environ``. That makes it easy to audit and impossible to accidentally log
a key from some deep adapter.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from trader.domain.models import RiskConfig

LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_THIS_IS_REAL_MONEY"


class ConfigError(Exception):
    """Raised when configuration is missing or unsafe."""


@dataclass(frozen=True)
class Settings:
    """Everything the app needs, resolved and validated."""

    project_root: Path
    mode: str  # "paper" | "live"
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_paper: bool
    telegram_bot_token: str
    telegram_chat_id: str
    anthropic_model: str
    db_path: Path
    halt_file: Path
    strategy_file: Path
    universe: tuple[str, ...]
    benchmark: str
    history_days: int
    risk: RiskConfig
    timezone: str
    propose_local_times: tuple[str, ...]
    weekly_target_pct: Decimal
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


def find_project_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` until we find config.toml."""
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "config.toml").exists():
            return candidate
    # Fall back to the package's grandparent (src/trader -> repo root) for installed use.
    pkg_root = Path(__file__).resolve().parents[2]
    if (pkg_root / "config.toml").exists():
        return pkg_root
    raise ConfigError("Could not find config.toml — run from inside the trading-agent folder.")


def load_settings(root: Path | None = None, *, require_broker: bool = True) -> Settings:
    """Read .env + config.toml and return validated ``Settings``.

    Args:
        root: project root; auto-detected if omitted.
        require_broker: set False for commands that don't need broker keys (e.g. telegram-test).
    """
    root = root or find_project_root()
    load_dotenv(root / ".env", override=False)

    with (root / "config.toml").open("rb") as f:
        cfg = tomllib.load(f)

    risk_cfg = cfg.get("risk", {})
    cap_raw = risk_cfg.get("capital_cap")
    capital_cap = Decimal(str(cap_raw)) if cap_raw not in (None, 0, "0", "") else None
    risk = RiskConfig(
        capital_cap=capital_cap,
        max_position_pct=Decimal(str(risk_cfg.get("max_position_pct", "0.25"))),
        max_order_notional=Decimal(str(risk_cfg.get("max_order_notional", "400"))),
        max_orders_per_proposal=int(risk_cfg.get("max_orders_per_proposal", 3)),
        min_cash_buffer_pct=Decimal(str(risk_cfg.get("min_cash_buffer_pct", "0.10"))),
        max_daily_loss_pct=Decimal(str(risk_cfg.get("max_daily_loss_pct", "0.03"))),
        max_limit_distance_pct=Decimal(str(risk_cfg.get("max_limit_distance_pct", "0.03"))),
        allow_fractional=bool(risk_cfg.get("allow_fractional", True)),
        allow_shorting=bool(risk_cfg.get("allow_shorting", False)),
        proposal_ttl=timedelta(hours=float(risk_cfg.get("proposal_ttl_hours", 7))),
        blocked_symbols=frozenset(s.upper() for s in risk_cfg.get("blocked_symbols", [])),
    )

    alpaca_paper = _env_bool("ALPACA_PAPER", default=True)
    live_confirm = os.environ.get("TRADER_LIVE_CONFIRM", "").strip()
    if not alpaca_paper and live_confirm != LIVE_CONFIRM_PHRASE:
        raise ConfigError(
            "ALPACA_PAPER=false but TRADER_LIVE_CONFIRM is not set to the confirmation phrase. "
            f"To trade real money, set TRADER_LIVE_CONFIRM={LIVE_CONFIRM_PHRASE} in .env. "
            "(This friction is deliberate.)"
        )
    mode = "paper" if alpaca_paper else "live"

    api_key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if require_broker and not (api_key and secret):
        raise ConfigError("ALPACA_API_KEY / ALPACA_SECRET_KEY missing — copy .env.example to .env and fill them in.")

    universe_cfg = cfg.get("universe", {})
    schedule_cfg = cfg.get("schedule", {})
    goal_cfg = cfg.get("goal", {})

    db_path = Path(os.environ.get("TRADER_DB_PATH", "data/journal.db"))
    if not db_path.is_absolute():
        db_path = root / db_path

    return Settings(
        project_root=root,
        mode=mode,
        alpaca_api_key=api_key,
        alpaca_secret_key=secret,
        alpaca_paper=alpaca_paper,
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        anthropic_model=os.environ.get("TRADER_MODEL", "claude-opus-5").strip(),
        db_path=db_path,
        halt_file=root / "HALT",
        strategy_file=root / "STRATEGY.md",
        universe=tuple(s.upper() for s in universe_cfg.get("symbols", [])),
        benchmark=str(universe_cfg.get("benchmark", "SPY")).upper(),
        history_days=int(universe_cfg.get("history_days", 60)),
        risk=risk,
        timezone=str(schedule_cfg.get("timezone", "America/New_York")),
        propose_local_times=tuple(str(x) for x in schedule_cfg.get("propose_local_times", ["09:45"])),
        weekly_target_pct=Decimal(str(goal_cfg.get("weekly_return_target_pct", "0"))),
    )


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
