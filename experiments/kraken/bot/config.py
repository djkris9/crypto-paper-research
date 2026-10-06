from dataclasses import dataclass

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

