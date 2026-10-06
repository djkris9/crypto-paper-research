"""Separate frozen four-hour research book; public GETs and simulated orders only."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time
from .binance import Binance
from .config import Config
from .execution import PaperBroker
from .indicators import Candle, features
from .lock import process_lock
from .store import Store
from .strategy import Signal, plan, risk_reason, quote_reason, portfolio_guard

FOUR_HOURS=14400000
EXPERIMENT="slow-breakout-4h-v1"
SPEC={"experiment":EXPERIMENT,"symbols":["BTCUSDT","ETHUSDT","SOLUSDT"],"channel_bars":20,"timeframe":"4h","volume_min":1.,"cost_headroom":.006,"ema_direction":50,"stop_atr":3.,"reward_risk":3.,"risk_fraction":.0025,"max_notional":1000.,"max_positions":2,"daily_loss":.02,"reserved_risk":.01,"peak_drawdown":.05,"mode":"paper","ai":False,"macro_gate":False,"signal_age_limit_ms":300000}
SPEC_HASH=hashlib.sha256(json.dumps(SPEC,sort_keys=True).encode()).hexdigest()

class PublicMarket(Binance):
    ALLOWED={"/fapi/v1/time","/fapi/v1/klines","/fapi/v1/exchangeInfo","/fapi/v1/premiumIndex","/fapi/v1/fundingRate","/fapi/v1/ticker/bookTicker"}
    def __init__(self):super().__init__(demo=False)
    def call(self,method,path,params=None,signed=False):
        if method!="GET" or signed or path not in self.ALLOWED:
            raise RuntimeError("Four-hour research runner allows public market GETs only")
        return super().call(method,path,params,signed=False)
    def interval_candles(self,symbol,interval):
        if interval not in {"15m","4h"}:raise ValueError("Unsupported research interval")
        now=int(time.time()*1000)+self.offset
        rows=self.call("GET","/fapi/v1/klines",{"symbol":symbol,"interval":interval,"limit":202})
        return [Candle(int(r[6]),*map(float,(r[1],r[2],r[3],r[4],r[5]))) for r in rows if int(r[6])<now][-201:]

class NoAlerts:
    def send(self,message):pass

def configuration(db,poll_seconds=60):
    if not math.isfinite(poll_seconds) or not 10<=poll_seconds<=300:raise ValueError("Poll must be10-300seconds")
    # Explicit construction: existing .env, credentials and AI settings cannot change this book.
    return Config(mode="paper",symbols=tuple(SPEC["symbols"]),db=str(db),paper_balance=10000.,risk_fraction=.0025,daily_loss=.02,max_positions=2,max_notional=1000.,max_total_risk=.01,atr_stop=3.,reward_risk=3.,poll_seconds=int(poll_seconds),calendar="file",calendar_file="",accept_partial_calendar=False,blackout_minutes=30,ai_enabled=False,ai_model="",ai_calls_per_day=1,ai_min_confidence=.65,max_drawdown=.05,paper_funding=True)

def claim_ledger(store,config):
    prior=store.get("experiment")
    populated=store.db.execute("SELECT count(*) FROM state").fetchone()[0] or store.db.execute("SELECT count(*) FROM events").fetchone()[0]
    if prior!=EXPERIMENT and populated:raise RuntimeError("Use a new separate four-hour ledger; existing books cannot be adopted")
    if store.get("strategy_spec_sha256",SPEC_HASH)!=SPEC_HASH:raise RuntimeError("Ledger strategy specification changed")
    store.set("experiment",EXPERIMENT);store.set("mode","paper");store.set("strategy_spec_sha256",SPEC_HASH)
    store.set("runtime_configuration",asdict(config))

def slow_features(bars):
    if len(bars)<201:raise ValueError("Need201complete four-hour candles")
    f=features(bars,interval_ms=FOUR_HOURS)
    f.update(channel_high=max(c.high for c in bars[-21:-1]),channel_low=min(c.low for c in bars[-21:-1]),previous_price=bars[-2].close,previous_channel_high=max(c.high for c in bars[-22:-2]),previous_channel_low=min(c.low for c in bars[-22:-2]))
    return f

def decide_slow(f,blocked=False):
    if blocked:return Signal("NO_TRADE","risk, funding or market data blocked")
    keys=("price","atr","ema50","volume_ratio","channel_high","channel_low","previous_price","previous_channel_high","previous_channel_low")
    if not all(math.isfinite(f.get(k,math.nan)) for k in keys) or f["price"]<=0 or f["atr"]<=0 or f["volume_ratio"]<1 or 3*f["atr"]/f["price"]<.006:
        return Signal("NO_TRADE","invalid inputs, volume or cost headroom")
    if f["price"]>f["channel_high"] and f["previous_price"]<=f["previous_channel_high"] and f["price"]>f["ema50"]:
        return Signal("LONG","four-hour channel breakout")
    if f["price"]<f["channel_low"] and f["previous_price"]>=f["previous_channel_low"] and f["price"]<f["ema50"]:
        return Signal("SHORT","four-hour channel breakout")
    return Signal("NO_TRADE","no new four-hour channel crossing")

def fill_plan(side,quote_price,atr,equity,config,filters):
    actual=quote_price*(1.0002 if side=="LONG" else .9998)
    trade=plan(side,actual,atr,equity,config,filters)
    trade["entry"]=quote_price  # PaperBroker applies exactly the modeled fill above.
    return trade

def synthetic_bars(now_ms,symbol,interval_ms):
    base={"BTCUSDT":60000.,"ETHUSDT":3000.,"SOLUSDT":150.}[symbol]
    end=now_ms//interval_ms*interval_ms-1
    return [Candle(end-(200-i)*interval_ms,base*(1+.0003*i),base*(1+.0003*i)+base*.002,base*(1+.0003*i)-base*.002,base*(1+.0003*i),100.) for i in range(201)]

def cycle(config,store,exchange,broker,offline=False,now_ms=None):
    fixed_now=now_ms
    clock=(lambda:fixed_now) if fixed_now is not None else (lambda:int(time.time()*1000)+getattr(exchange,"offset",0))
    now_ms=clock()
    failures=[];signals={};fresh_marks=set()
    if not offline:
        try:
            if not broker.reconcile_funding(exchange,now_ms):failures.append("pending funding")
        except Exception as error:
            failures.append("funding reconciliation unavailable")
            store.log("FUNDING_UNAVAILABLE",error_type=type(error).__name__)
    for symbol in config.symbols:
        try:
            exits=synthetic_bars(now_ms,symbol,900000) if offline else exchange.interval_candles(symbol,"15m")
            features(exits)
            now_ms=clock()
            if not 0<=now_ms-exits[-1].close_time<=1200000:raise ValueError("Stale/future exit bars")
            mark=exits[-1].close if offline else float(exchange.call("GET","/fapi/v1/premiumIndex",{"symbol":symbol})["markPrice"])
            if not math.isfinite(mark) or mark<=0:raise ValueError("Invalid mark")
            held=next((p for p in broker.positions() if p["symbol"]==symbol),None)
            funding=[]
            if held and not offline:
                try:funding=exchange.funding_history(symbol,held["entry_time_ms"]+1,now_ms)
                except Exception as error:
                    funding=None;failures.append(symbol+":funding")
                    store.log("FUNDING_UNAVAILABLE",symbol,error_type=type(error).__name__)
            try:broker.mark(symbol,exits,mark,funding=funding,observed_at_ms=now_ms)
            except ValueError:
                store.halt("Four-hour paper funding validation failed")
                failures.append(symbol+":funding-ledger")
                broker.mark(symbol,exits,mark,funding=None,observed_at_ms=now_ms)
            fresh_marks.add(symbol)
            bars=synthetic_bars(now_ms,symbol,FOUR_HOURS) if offline else exchange.interval_candles(symbol,"4h")
            f=slow_features(bars)
            now_ms=clock()
            if not 0<=now_ms-bars[-1].close_time<=FOUR_HOURS+300000:raise ValueError("Stale/future signal bars")
            signals[symbol]=(bars[-1].close_time,f)
        except Exception as error:
            failures.append(symbol)
            store.log("DATA_ERROR",symbol,error_type=type(error).__name__)
    if any(p["symbol"] not in fresh_marks for p in broker.positions()):
        store.log("VALUATION_UNAVAILABLE",reason="A held symbol lacks a fresh mark")
        return {"mode":"paper","experiment":EXPERIMENT,"entries":0,"valuation_available":False,"market_errors":failures}
    now_ms=clock()
    equity=broker.equity()
    peak,drawdown=portfolio_guard(store,equity,config.max_drawdown)
    if peak is None or drawdown is None:raise RuntimeError("Equity unavailable")
    day=datetime.fromtimestamp(now_ms/1000,timezone.utc).date().isoformat()
    baseline_key="daily_equity:"+day
    baseline=store.get(baseline_key)
    if baseline is None:baseline=equity;store.set(baseline_key,baseline)
    if equity<=baseline*(1-config.daily_loss):store.set("daily_block:"+day,True)
    blocked=bool(failures or store.get("halt") or store.get("daily_block:"+day) or store.get("paper_funding_pending",[]))
    entries=0
    for symbol,(stamp,f) in signals.items():
        key="last_slow_bar:"+symbol
        prior=store.get(key)
        if prior is not None and stamp<=prior:continue
        store.set(key,stamp)  # Crash/failure/restart cannot replay this setup.
        signal=decide_slow(f,blocked)
        if now_ms-stamp>SPEC["signal_age_limit_ms"]:signal=Signal("NO_TRADE","setup older than five minutes; wait for next close")
        if any(p["symbol"]==symbol for p in broker.positions()):signal=Signal("NO_TRADE","already holding symbol")
        if signal.side!="NO_TRADE":
            try:
                filters={"step":"0.001","tick":"0.01","min_qty":.001,"max_qty":1000,"min_notional":5} if offline else exchange.filters(symbol)
                quote={"mark":f["price"],"bid":f["price"],"ask":f["price"],"mark_time":now_ms,"book_time":now_ms} if offline else exchange.entry_quote(symbol)
                now_ms=clock()
                reason=quote_reason(signal.side,f["price"],quote,now_ms,config)
                if now_ms-stamp>SPEC["signal_age_limit_ms"]:reason="setup exceeded five-minute limit during quote retrieval"
                store.log("ENTRY_QUOTE",symbol,quote=quote,reference=f["price"],rejection=reason)
                if reason:signal=Signal("NO_TRADE",reason)
                else:
                    broker.prices[symbol]=quote["mark"]
                    equity=broker.equity();peak,drawdown=portfolio_guard(store,equity,config.max_drawdown)
                    trade=fill_plan(signal.side,quote["ask"] if signal.side=="LONG" else quote["bid"],f["atr"],equity,config,filters)
                    reason=risk_reason(config,equity,baseline,broker.positions(),trade,store.get("halt"),peak)
                    if reason:signal=Signal("NO_TRADE",reason)
                    else:
                        broker.enter(symbol,trade,now_ms//900000*900000-1,entered_at_ms=now_ms)
                        entries+=1
            except Exception as error:signal=Signal("NO_TRADE",type(error).__name__+" during paper execution")
        store.log("DECISION",symbol,side=signal.side,reason=signal.reason,features=f,signal_close_ms=stamp,experiment=EXPERIMENT)
        print(f"{symbol}: {signal.side} ({signal.reason})")
    equity=broker.equity();peak,drawdown=portfolio_guard(store,equity,config.max_drawdown)
    status={"mode":"paper","experiment":EXPERIMENT,"equity":equity,"positions":len(broker.positions()),"entries":entries,"drawdown":drawdown,"halt":store.get("halt"),"market_errors":failures,"checked_at":datetime.fromtimestamp(now_ms/1000,timezone.utc).isoformat(),"valuation_available":True}
    store.log("EQUITY",**status)
    print(json.dumps(status))
    return status

def main():
    parser=argparse.ArgumentParser(description="Frozen four-hour public-data paper research only")
    parser.add_argument("--db",default="data/slow-paper.sqlite")
    parser.add_argument("--poll",type=int,default=60)
    parser.add_argument("--once",action="store_true")
    parser.add_argument("--offline",action="store_true")
    parser.add_argument("--preflight",action="store_true",help="Public-data connectivity check; no ledger or trading")
    parser.add_argument("--checkpoint",help="Portable paper-only JSON state; used by scheduled jobs")
    args=parser.parse_args()
    if args.preflight:
        if args.offline:parser.error("Preflight must use public network data")
        market=PublicMarket();market.sync()
        for symbol in SPEC["symbols"]:
            slow_features(market.interval_candles(symbol,"4h"))
            features(market.interval_candles(symbol,"15m"))
            market.filters(symbol);quote=market.entry_quote(symbol)
            if not all(math.isfinite(quote[k]) and quote[k]>0 for k in ("mark","bid","ask")):raise RuntimeError("Invalid public quote")
        print(json.dumps({"public_connectivity":True,"mode":"paper","ledger_created":False}))
        return
    if args.offline and args.checkpoint:parser.error("Offline smoke cannot write the forward checkpoint")
    db="data/slow-offline.sqlite" if args.offline else args.db
    config=configuration(db,args.poll)
    store=Store(config.db)
    with process_lock(store):
        if args.checkpoint:
            from .slow_state import restore_checkpoint
            restore_checkpoint(store,args.checkpoint)
        claim_ledger(store,config)
        broker=PaperBroker(config,store,NoAlerts())
        exchange=None if args.offline else PublicMarket()
        if exchange:exchange.sync()
        try:
            while True:
                try:cycle(config,store,exchange,broker,offline=args.offline)
                except KeyboardInterrupt:break
                except Exception as error:
                    store.log("CYCLE_ERROR",error_type=type(error).__name__)
                    if args.once:raise
                if args.once:break
                time.sleep(config.poll_seconds)
        finally:
            if args.checkpoint:
                from .slow_state import export_checkpoint
                export_checkpoint(store,args.checkpoint)
            store.db.close()
if __name__=="__main__":main()
