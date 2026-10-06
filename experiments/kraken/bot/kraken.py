"""Public Kraken USD linear perpetual data. No credentials, orders or redirects."""
from datetime import datetime, timezone
from decimal import Decimal
import math
import time
import requests
from .indicators import Candle, features

HOUR = 3600000
SYMBOLS = {"BTCUSD": "PF_XBTUSD", "ETHUSD": "PF_ETHUSD", "SOLUSD": "PF_SOLUSD"}
HOST = "https://futures.kraken.com"

def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Server timestamp requires timezone")
    return int(result.timestamp() * 1000)

def number(value, positive=False):
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError("Invalid public market number")
    return result

class KrakenPublic:
    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.offset = 0
        self._instruments = None
        self.allowed = {
            "/derivatives/api/v3/tickers": set(),
            "/derivatives/api/v3/instruments": set(),
            "/derivatives/api/v3/orderbook": {"symbol"},
            "/derivatives/api/v3/historical-funding-rates": {"symbol"},
        }
        for instrument in SYMBOLS.values():
            for interval in ("4h", "15m"):
                self.allowed[f"/api/charts/v1/trade/{instrument}/{interval}"] = {"count"}

    def call(self, method, path, params=None, signed=False):
        params = dict(params or {})
        if method != "GET" or signed or path not in self.allowed or set(params) - self.allowed[path]:
            raise RuntimeError("Kraken research accepts allowlisted public GETs only")
        if "symbol" in self.allowed[path] and params.get("symbol") not in SYMBOLS.values():
            raise ValueError("Unsupported Kraken instrument")
        if "count" in self.allowed[path] and params.get("count") != 202:
            raise ValueError("Research candles require fixed 202-bar request")
        try:
            response = self.session.get(HOST + path, params=params, timeout=(5, 15), allow_redirects=False)
        except requests.RequestException:
            raise RuntimeError("Kraken public network failure") from None
        if response.status_code != 200:
            raise RuntimeError(f"Kraken HTTP {response.status_code} on {path}")
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("result", "success") != "success":
            raise ValueError("Invalid Kraken public response")
        return payload

    def sync(self):
        before = int(time.time() * 1000)
        payload = self.call("GET", "/derivatives/api/v3/tickers")
        after = int(time.time() * 1000)
        delta = timestamp(payload["serverTime"]) - (before + after) // 2
        if abs(delta) > 30000 or after - before > 10000:
            raise ValueError("Public clock stale or local clock inaccurate")
        self.offset = delta

    def interval_candles(self, symbol, interval):
        if interval not in {"4h", "15m"} or symbol not in SYMBOLS:
            raise ValueError("Unsupported research symbol/interval")
        interval_ms = 14400000 if interval == "4h" else 900000
        rows = self.call("GET", f"/api/charts/v1/trade/{SYMBOLS[symbol]}/{interval}", {"count": 202})["candles"]
        now = int(time.time() * 1000) + self.offset
        bars = []
        for row in rows:
            start = int(row["time"])
            if start < 0 or start % interval_ms:
                raise ValueError("Unaligned Kraken candle")
            if start + interval_ms > now:
                continue
            bars.append(Candle(start + interval_ms - 1, *(number(row[k]) for k in ("open", "high", "low", "close", "volume"))))
        bars.sort(key=lambda c: c.close_time)
        bars = bars[-201:]
        if len(bars) != 201:
            raise ValueError("Need 201 completed Kraken candles")
        features(bars, interval_ms=interval_ms)
        if not 0 <= now - bars[-1].close_time <= interval_ms + 300000:
            raise ValueError("Stale Kraken candles")
        return bars

    def filters(self, symbol):
        if self._instruments is None:
            rows = self.call("GET", "/derivatives/api/v3/instruments")["instruments"]
            self._instruments = {row["symbol"]: row for row in rows}
        row = self._instruments[SYMBOLS[symbol]]
        if not row["tradeable"] or row["type"] != "flexible_futures" or row["quote"] != "USD" or number(row["contractSize"]) != 1:
            raise ValueError("Expected live linear USD contract with one base unit")
        precision = row["contractValueTradePrecision"]
        if not isinstance(precision, int) or not 0 <= precision <= 8:
            raise ValueError("Unsupported quantity precision")
        step = str(Decimal(1).scaleb(-precision))
        return {"step": step, "min_qty": float(step), "max_qty": number(row["maxPositionSize"], True),
                "tick": str(number(row["tickSize"], True)), "min_notional": 0.0}

    def _ticker(self, symbol):
        payload = self.call("GET", "/derivatives/api/v3/tickers")
        row = next(row for row in payload["tickers"] if row["symbol"] == SYMBOLS[symbol])
        stamp = timestamp(payload["serverTime"])
        if row["suspended"] or row.get("postOnly") or not -1000 <= int(time.time()*1000) + self.offset - stamp <= 15000:
            raise ValueError("Unavailable or stale Kraken ticker")
        return row, stamp

    def mark_price(self, symbol):
        row, _ = self._ticker(symbol)
        return number(row["markPrice"], True)

    def entry_quote(self, symbol):
        row, stamp = self._ticker(symbol)
        payload = self.call("GET", "/derivatives/api/v3/orderbook", {"symbol": SYMBOLS[symbol]})
        book = payload["orderBook"]
        bids = [number(level[0], True) for level in book["bids"] if number(level[1]) > 0]
        asks = [number(level[0], True) for level in book["asks"] if number(level[1]) > 0]
        if not bids or not asks or max(bids) > min(asks):
            raise ValueError("Empty or crossed Kraken order book")
        return {"mark": number(row["markPrice"], True), "bid": max(bids), "ask": min(asks),
                "mark_time": stamp, "book_time": timestamp(payload["serverTime"])}

    def funding_history(self, symbol, start_ms, end_ms):
        if start_ms > end_ms:
            return []
        if end_ms - start_ms > 31 * 86400000:
            raise ValueError("Funding window exceeds research bounds")
        payload = self.call("GET", "/derivatives/api/v3/historical-funding-rates", {"symbol": SYMBOLS[symbol]})
        rows = {}
        first = (int(start_ms) - 1) // HOUR * HOUR
        last = (int(end_ms) - 1) // HOUR * HOUR
        for row in payload["rates"]:
            stamp = timestamp(row["timestamp"])
            if not first <= stamp <= last:
                continue
            if stamp % HOUR:
                raise ValueError("Unaligned hourly funding record")
            rate = number(row["fundingRate"])
            if stamp in rows and rows[stamp] != rate:
                raise ValueError("Conflicting hourly funding rates")
            rows[stamp] = rate
        if set(rows) != set(range(first, last + HOUR, HOUR)):
            raise ValueError("Hourly funding history incomplete; block new entries")
        return [dict(symbol=symbol, hour_start_ms=stamp, hourly_usd_per_unit=rows[stamp]) for stamp in sorted(rows)]
