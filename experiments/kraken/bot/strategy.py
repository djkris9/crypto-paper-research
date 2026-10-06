from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import math


@dataclass
class Signal:
    side: str
    reason: str


def decide(f, blocked=False, ai=None, ai_required=False, confidence=.65):
    if blocked:
        return Signal("NO_TRADE", "macro blackout or unavailable context")
    if not all(math.isfinite(v) for v in f.values()) or f["atr"] <= 0 or f["volume_ratio"] < 1.2:
        return Signal("NO_TRADE", "invalid volatility or weak volume")
    side = "NO_TRADE"
    if f["price"] > f["ema20"] > f["ema50"] and 50 <= f["rsi"] <= 68:
        side = "LONG"
    elif f["price"] < f["ema20"] < f["ema50"] and 32 <= f["rsi"] <= 50:
        side = "SHORT"
    if side == "NO_TRADE":
        return Signal(side, "trend and RSI do not agree")
    if ai_required and (not ai or ai["bias"] != side or ai["confidence"] < confidence):
        return Signal("NO_TRADE", "AI unavailable, uncertain or disagrees")
    return Signal(side, "trend, RSI and volume agree")


def rounded(value, step, up=False):
    d, s = Decimal(str(value)), Decimal(str(step))
    return (d/s).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN)*s


def plan(side, price, atr, equity, config, filters):
    distance = config.atr_stop * atr
    if not all(math.isfinite(x) and x > 0 for x in (price, distance, equity)):
        raise ValueError("Invalid sizing inputs")
    stop = rounded(price-distance if side == "LONG" else price+distance, filters["tick"], up=side == "SHORT")
    take = rounded(price+distance*config.reward_risk if side == "LONG" else price-distance*config.reward_risk, filters["tick"], up=side == "LONG")
    if stop <= 0 or take <= 0:
        raise ValueError("Invalid protection prices")
    # Reserve a buffer for fees and adverse fills; SL still cannot guarantee a loss ceiling.
    unit_risk = abs(price-float(stop)) + price*.002
    quantity = rounded(min(equity*config.risk_fraction/unit_risk, config.max_notional/price, equity*.9/price, filters["max_qty"]), filters["step"])
    if quantity < Decimal(str(filters["min_qty"])) or float(quantity)*price < filters["min_notional"]:
        raise ValueError("Position below exchange minimum")
    return {"side":side, "quantity":str(quantity), "stop":str(stop), "take":str(take), "entry":price, "risk":float(quantity)*unit_risk}


def risk_reason(config, equity, baseline, positions, trade, halted, peak=None):
    if halted:
        return "persistent safety halt"
    if equity <= 0 or equity <= baseline*(1-config.daily_loss):
        return "daily loss limit"
    if "quantity" in trade and sum(float(p["quantity"])*p["entry"] for p in positions)+float(trade["quantity"])*trade["entry"] > equity*.9:
        return "available collateral budget"
    if len(positions) >= config.max_positions:
        return "maximum simultaneous positions"
    if sum(p["risk"] for p in positions)+trade["risk"] > equity*config.max_total_risk:
        return "total stop risk budget"
    if peak is not None and sum(p["risk"] for p in positions)+trade["risk"] > equity-peak*(1-config.max_drawdown):
        return "remaining portfolio drawdown budget"
    if sum(p["risk"] for p in positions)+trade["risk"] > equity-baseline*(1-config.daily_loss):
        return "remaining daily loss budget"
    return None


def selective_reason(f, atr_stop=2., cost_per_side=.0007):
    """Fixed research hypothesis: trend separation, limited stretch, cost headroom."""
    values = [f.get(k, math.nan) for k in ("price", "ema20", "ema50", "atr")]
    if not all(math.isfinite(v) for v in values) or f["price"] <= 0 or f["atr"] <= 0:
        return "invalid selective entry inputs"
    if atr_stop * f["atr"] / f["price"] < 3 * 2 * cost_per_side:
        return "price movement too small relative to costs"
    if abs(f["ema20"] - f["ema50"]) < .5 * f["atr"]:
        return "trend separation too weak"
    if abs(f["price"] - f["ema20"]) > f["atr"]:
        return "entry too stretched from trend"
    return None


def portfolio_guard(store, equity, max_drawdown):
    """Persist the peak and latch a breach; midnight/restart cannot clear it."""
    if not math.isfinite(equity) or equity <= 0:
        if not store.get("halt"):
            store.halt("Invalid or depleted equity")
        return store.get("equity_peak"), None
    peak = store.get("equity_peak", equity)
    if not isinstance(peak, (int, float)) or not math.isfinite(peak) or peak <= 0:
        if not store.get("halt"):
            store.halt("Invalid persisted equity peak")
        return None, None
    peak = max(peak, equity)
    store.set("equity_peak", peak)
    drawdown = max(0., 1 - equity / peak)
    # Compare equity rather than rounded drawdown at the threshold boundary.
    if equity <= peak * (1 - max_drawdown):
        if not store.get("portfolio_drawdown_halt"):
            store.set("portfolio_drawdown_halt", True)
            store.log("PORTFOLIO_DRAWDOWN_HALT", equity=equity, peak=peak, drawdown=drawdown, limit=max_drawdown)
        if not store.get("halt"):
            store.halt("Portfolio drawdown limit reached")
    if store.get("portfolio_drawdown_halt") and not store.get("halt"):
        store.halt("Portfolio drawdown limit remains latched")
    return peak, drawdown


def entry_event_reason(side, previous, trigger="continuous"):
    """A pullback entry is a new EMA20 reclaim, not a continuously true trend."""
    if trigger == "continuous":
        return None
    if trigger != "pullback" or side not in {"LONG", "SHORT"}:
        return "invalid entry trigger"
    if previous is None or not all(math.isfinite(previous.get(k, math.nan)) for k in ("price", "ema20")):
        return "previous closed-candle context unavailable"
    reclaimed = previous["price"] <= previous["ema20"] if side == "LONG" else previous["price"] >= previous["ema20"]
    return None if reclaimed else "waiting for a new trend pullback"


def quote_reason(side, signal_price, quote, now_ms, config):
    keys=("mark","bid","ask","mark_time","book_time")
    if side not in {"LONG","SHORT"} or not math.isfinite(signal_price) or signal_price <= 0:
        return "invalid entry reference"
    if not all(isinstance(quote.get(k),(int,float)) and math.isfinite(quote[k]) and quote[k]>0 for k in keys):
        return "invalid execution quote"
    if quote["ask"] < quote["bid"]:
        return "crossed execution quote"
    if any(not -1000 <= now_ms-quote[k] <= config.quote_max_age_ms for k in ("mark_time","book_time")):
        return "stale execution quote"
    mid=(quote["bid"]+quote["ask"])/2
    if (quote["ask"]-quote["bid"])/mid > config.max_spread:
        return "spread exceeds entry limit"
    expected=quote["ask"] if side=="LONG" else quote["bid"]
    if abs(expected/signal_price-1)>config.max_entry_deviation:
        return "price moved too far from setup"
    if abs(quote["mark"]/mid-1)>config.max_entry_deviation:
        return "mark and executable quote disagree"
    return None


def higher_trend_reason(side, higher, now_ms):
    if not higher or not all(math.isfinite(higher.get(k,math.nan)) for k in ("price","ema20","ema50","closed_at")):
        return "higher trend unavailable"
    if not 0 <= now_ms-higher["closed_at"] <= 14400000+300000:
        return "higher trend stale or future"
    agree=(higher["price"]>higher["ema20"]>higher["ema50"]) if side=="LONG" else (higher["price"]<higher["ema20"]<higher["ema50"]) if side=="SHORT" else False
    return None if agree else "4-hour trend does not agree"
