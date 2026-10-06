from dataclasses import dataclass
import math


@dataclass
class Candle:
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def ema(values, period):
    value = sum(values[:period]) / period
    for x in values[period:]:
        value += 2 / (period + 1) * (x - value)
    return value


def wilder(values, period=14):
    value = sum(values[:period]) / period
    for x in values[period:]:
        value = (value * (period - 1) + x) / period
    return value


def features(candles, interval_ms=900000):
    if len(candles) < 100:
        raise ValueError("Need at least 100 closed candles")
    if any(not all(math.isfinite(v) for v in (c.open,c.high,c.low,c.close,c.volume)) or c.low <= 0 or c.high < max(c.open,c.close,c.low) or c.low > min(c.open,c.close) or c.volume < 0 for c in candles):
        raise ValueError("Invalid candle values")
    if any(b.close_time - a.close_time != interval_ms for a,b in zip(candles,candles[1:])):
        raise ValueError("Missing or unordered candles")
    # Keep the original 200-bar reseeding even when one extra bar is fetched
    # to calculate the previous closed candle independently.
    candles = candles[-200:]
    prices = [c.close for c in candles]
    diffs = [b-a for a,b in zip(prices,prices[1:])]
    gain = wilder([max(d,0) for d in diffs])
    loss = wilder([max(-d,0) for d in diffs])
    rsi = 50 if gain == loss == 0 else 100 if loss == 0 else 100 - 100/(1+gain/loss)
    tr = [max(c.high-c.low, abs(c.high-p.close), abs(c.low-p.close)) for p,c in zip(candles,candles[1:])]
    avg_volume = sum(c.volume for c in candles[-21:-1])/20
    return {"price":prices[-1], "ema20":ema(prices,20), "ema50":ema(prices,50), "rsi":rsi,
            "atr":wilder(tr), "volume_ratio":candles[-1].volume / avg_volume if avg_volume else 0}
