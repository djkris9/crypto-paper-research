import hashlib
import hmac
import os
import time
from urllib.parse import urlencode
import requests
from .indicators import Candle

DEMO_URL = "https://demo-fapi.binance.com"
PUBLIC_URL = "https://fapi.binance.com"


class Binance:
    def __init__(self, demo=False):
        self.demo = demo
        self.base = DEMO_URL if demo else PUBLIC_URL
        self.session = requests.Session()
        self.offset = 0
        self._filters = None

    def call(self, method, path, params=None, signed=False):
        if method != "GET" and not self.demo:
            raise RuntimeError("Order mutations require hardcoded demo host")
        params = dict(params or {})
        headers = {}
        if signed:
            if not self.demo:
                raise RuntimeError("Private APIs only allowed on demo")
            params.update(timestamp=int(time.time()*1000)+self.offset, recvWindow=5000)
            query = urlencode(params)
            params["signature"] = hmac.new(os.environ["BINANCE_API_SECRET"].encode(), query.encode(), hashlib.sha256).hexdigest()
            headers["X-MBX-APIKEY"] = os.environ["BINANCE_API_KEY"]
        # Never retry mutations: a timeout does not establish that an order failed.
        try:
            response = self.session.request(method, self.base+path, params=params, headers=headers, timeout=(5,15))
        except requests.RequestException:
            raise RuntimeError(f"Binance network failure on {path}; mutation outcome may be unknown") from None
        if not response.ok:
            # Do not print URLs, signatures, headers or raw server messages.
            raise RuntimeError(f"Binance HTTP {response.status_code} on {path}")
        return response.json()

    def sync(self):
        self.offset = int(self.call("GET", "/fapi/v1/time")["serverTime"]) - int(time.time()*1000)

    def candles(self, symbol):
        now = int(time.time()*1000)+self.offset
        rows = self.call("GET", "/fapi/v1/klines", {"symbol":symbol, "interval":"15m", "limit":202})
        return [Candle(int(r[6]), *map(float, (r[1],r[2],r[3],r[4],r[5]))) for r in rows if int(r[6]) < now][-201:]

    def filters(self, symbol):
        if self._filters is None:
            self._filters = {s["symbol"]:s for s in self.call("GET", "/fapi/v1/exchangeInfo")["symbols"]}
        info = self._filters[symbol]
        if info["status"] != "TRADING":
            raise RuntimeError("Symbol is not trading")
        f = {x["filterType"]:x for x in info["filters"]}
        lot = f["MARKET_LOT_SIZE"]
        return {"step":lot["stepSize"], "min_qty":float(lot["minQty"]), "max_qty":float(lot["maxQty"]),
                "tick":f["PRICE_FILTER"]["tickSize"], "min_notional":float(f["MIN_NOTIONAL"]["notional"])}

    def account(self):
        a = self.call("GET", "/fapi/v3/account", signed=True)
        usdt = next(x for x in a["assets"] if x["asset"] == "USDT")
        return float(usdt["walletBalance"])+float(usdt["unrealizedProfit"]), float(usdt["availableBalance"])

    def positions(self):
        return [p for p in self.call("GET", "/fapi/v3/positionRisk", signed=True) if float(p["positionAmt"]) != 0]

    def validate_mode(self):
        if self.call("GET", "/fapi/v1/positionSide/dual", signed=True)["dualSidePosition"]:
            raise RuntimeError("Use a dedicated one-way demo account")
        if self.call("GET", "/fapi/v1/multiAssetsMargin", signed=True)["multiAssetsMargin"]:
            raise RuntimeError("Disable multi-assets mode in the demo account")

    def market(self, symbol, side, quantity, client_id, reduce=False):
        p = {"symbol":symbol,"side":side,"type":"MARKET","quantity":quantity,"newClientOrderId":client_id,"newOrderRespType":"RESULT"}
        if reduce:
            p["reduceOnly"] = "true"
        return self.call("POST", "/fapi/v1/order", p, True)

    def protection(self, symbol, side, trigger, kind, client_id):
        return self.call("POST", "/fapi/v1/algoOrder", {"algoType":"CONDITIONAL", "symbol":symbol,"side":side,"type":kind,
                         "triggerPrice":trigger,"closePosition":"true","workingType":"MARK_PRICE","clientAlgoId":client_id}, True)

    def open_algos(self):
        return self.call("GET", "/fapi/v1/openAlgoOrders", signed=True)

    def cancel_algo(self, algo_id):
        return self.call("DELETE", "/fapi/v1/algoOrder", {"algoId":algo_id}, True)


    def funding_history(self, symbol, start_ms, end_ms):
        if start_ms > end_ms:
            return []
        results = {}
        cursor = int(start_ms)
        for _ in range(10):
            rows = self.call("GET", "/fapi/v1/fundingRate", {"symbol":symbol,"startTime":cursor,"endTime":int(end_ms),"limit":1000})
            if not isinstance(rows, list):
                raise ValueError("Invalid funding history response")
            for row in rows:
                stamp = int(row["fundingTime"])
                if row.get("symbol") != symbol or not cursor <= stamp <= end_ms:
                    raise ValueError("Unexpected funding record")
                key=(stamp,row.get("rateType","Regular"))
                if key in results and results[key] != row:
                    raise ValueError("Conflicting funding records")
                results[key] = row
            if len(rows) < 1000:
                return [results[k] for k in sorted(results)]
            next_cursor = max(int(r["fundingTime"]) for r in rows)
            if next_cursor <= cursor:
                raise ValueError("Funding pagination did not advance")
            cursor = next_cursor
        raise ValueError("Funding history exceeds bounded pagination")

    def entry_quote(self, symbol):
        mark = self.call("GET", "/fapi/v1/premiumIndex", {"symbol":symbol})
        book = self.call("GET", "/fapi/v1/ticker/bookTicker", {"symbol":symbol})
        return {"mark":float(mark["markPrice"]), "bid":float(book["bidPrice"]), "ask":float(book["askPrice"]),
                "mark_time":int(mark["time"]), "book_time":int(book["time"])}

    def higher_trend(self, symbol):
        from .indicators import features
        now=int(time.time()*1000)+self.offset
        rows=self.call("GET", "/fapi/v1/klines", {"symbol":symbol,"interval":"4h","limit":101})
        bars=[Candle(int(r[6]),*map(float,(r[1],r[2],r[3],r[4],r[5]))) for r in rows if int(r[6])<now][-100:]
        if len(bars)!=100:
            raise ValueError("Need 100 completed 4h candles")
        f=features(bars,interval_ms=14400000)
        return {"price":f["price"],"ema20":f["ema20"],"ema50":f["ema50"],"closed_at":bars[-1].close_time}
