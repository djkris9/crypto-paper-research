import os
from dataclasses import dataclass, replace
from dotenv import load_dotenv


def flag(name, default="false"):
    value = os.getenv(name, default).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


@dataclass(frozen=True)
class Config:
    mode: str
    symbols: tuple[str, ...]
    db: str
    paper_balance: float
    risk_fraction: float
    daily_loss: float
    max_positions: int
    max_notional: float
    max_total_risk: float
    atr_stop: float
    reward_risk: float
    poll_seconds: int
    calendar: str
    calendar_file: str
    accept_partial_calendar: bool
    blackout_minutes: int
    ai_enabled: bool
    ai_model: str
    ai_calls_per_day: int
    ai_min_confidence: float

    selective_entries: bool = False
    max_drawdown: float = .05
    entry_trigger: str = "continuous"
    higher_trend_filter: bool = False
    paper_funding: bool = True
    max_spread: float = .0005
    max_entry_deviation: float = .001
    quote_max_age_ms: int = 10000
    calendar_cache: str = "data/calendar-cache.sqlite"

    @classmethod
    def load(cls):
        load_dotenv()
        c = cls(os.getenv("MODE", "paper"), tuple(os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",")),
                os.getenv("DB_PATH", "data/bot.sqlite"), float(os.getenv("PAPER_BALANCE", "10000")),
                float(os.getenv("RISK_FRACTION", "0.0025")), float(os.getenv("DAILY_LOSS_FRACTION", "0.02")),
                int(os.getenv("MAX_POSITIONS", "2")), float(os.getenv("MAX_POSITION_NOTIONAL", "1000")),
                float(os.getenv("MAX_TOTAL_RISK_FRACTION", "0.01")), float(os.getenv("ATR_STOP", "2")),
                float(os.getenv("REWARD_RISK", "2")), int(os.getenv("POLL_SECONDS", "60")),
                os.getenv("CALENDAR_ADAPTER", "forexfactory"), os.getenv("CALENDAR_FILE", "data/calendar.json"),
                flag("ACCEPT_PARTIAL_CALENDAR"), int(os.getenv("NEWS_BLACKOUT_MINUTES", "30")),
                flag("AI_ENABLED"), os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite"),
                int(os.getenv("AI_CALLS_PER_DAY", "20")), float(os.getenv("AI_MIN_CONFIDENCE", "0.65")))
        c = replace(c, selective_entries=flag("SELECTIVE_ENTRIES"),
                    max_drawdown=float(os.getenv("MAX_DRAWDOWN_FRACTION", "0.05")),
                    entry_trigger=os.getenv("ENTRY_TRIGGER", "continuous"),
                    higher_trend_filter=flag("HIGHER_TREND_FILTER"),
                    paper_funding=flag("PAPER_FUNDING_ENABLED", "true"),
                    max_spread=float(os.getenv("MAX_SPREAD_FRACTION", "0.0005")),
                    max_entry_deviation=float(os.getenv("MAX_ENTRY_DEVIATION_FRACTION", "0.001")),
                    quote_max_age_ms=int(os.getenv("QUOTE_MAX_AGE_MS", "10000")),
                    calendar_cache=os.getenv("CALENDAR_CACHE_PATH", "data/calendar-cache.sqlite"))
        if not c.calendar_cache.strip():
            raise ValueError("CALENDAR_CACHE_PATH must not be empty")
        if not (0 < c.max_spread <= .005 and 0 < c.max_entry_deviation <= .01 and 1000 <= c.quote_max_age_ms <= 30000):
            raise ValueError("Invalid execution quote limits")
        if c.entry_trigger not in {"continuous", "pullback"}:
            raise ValueError("ENTRY_TRIGGER must be continuous or pullback")
        if c.mode not in {"paper", "demo"} or c.calendar not in {"forexfactory", "bls", "file"}:
            raise ValueError("Only paper/demo modes and forexfactory/bls/file calendars are supported")
        if not c.symbols or any(s not in {"BTCUSDT", "ETHUSDT", "SOLUSDT"} for s in c.symbols) or len(set(c.symbols)) != len(c.symbols):
            raise ValueError("Symbols must be unique BTCUSDT, ETHUSDT or SOLUSDT")
        if not (0 < c.risk_fraction <= .01 and 0 < c.daily_loss <= .05 and 0 < c.max_total_risk <= .05 and 0 < c.max_drawdown <= .10):
            raise ValueError("Risk fractions exceed allowed bounds")
        if not (1 <= c.max_positions <= 3 and c.max_notional > 0 and c.paper_balance > 0 and c.atr_stop > 0 and c.reward_risk >= 1 and c.poll_seconds >= 10 and c.blackout_minutes >= 0 and c.ai_calls_per_day >= 1 and 0 <= c.ai_min_confidence <= 1):
            raise ValueError("Invalid sizing, timing or AI configuration")
        if c.ai_enabled and (not flag("GEMINI_FREE_TIER_CONFIRMED") or not os.getenv("GEMINI_API_KEY")):
            raise ValueError("AI requires a billing-disabled free-tier project, GEMINI_FREE_TIER_CONFIRMED=true and a key")
        if c.mode == "demo" and (not flag("DEMO_TRADING_ENABLED") or not os.getenv("BINANCE_API_KEY") or not os.getenv("BINANCE_API_SECRET")):
            raise ValueError("Demo requires DEMO_TRADING_ENABLED=true and testnet credentials")
        return c
