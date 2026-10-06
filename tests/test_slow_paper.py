import json
import math
from pathlib import Path
import pytest
from bot.slow_paper import PublicMarket,NoAlerts,configuration,claim_ledger,slow_features,decide_slow,fill_plan,cycle,FOUR_HOURS
from bot.execution import PaperBroker
from bot.store import Store
from bot.strategy import Signal

@pytest.mark.parametrize("method,path,signed",[("POST","/fapi/v1/order",False),("GET","/fapi/v3/account",True),("GET","/fapi/v3/account",False),("DELETE","/fapi/v1/algoOrder",False)])
def test_gateway_cannot_reach_private_or_mutating_apis(method,path,signed):
    with pytest.raises(RuntimeError):PublicMarket().call(method,path,signed=signed)

def test_environment_cannot_enable_demo_or_ai(monkeypatch):
    monkeypatch.setenv("MODE","demo");monkeypatch.setenv("AI_ENABLED","true")
    c=configuration("separate.sqlite")
    assert c.mode=="paper" and not c.ai_enabled and c.atr_stop==3 and c.reward_risk==3
    assert c.risk_fraction==.0025 and c.max_positions==2 and c.max_notional==1000

def test_existing_paper_ledger_cannot_be_adopted(tmp_path):
    store=Store(tmp_path/"old.sqlite");store.set("paper_cash",12345.)
    with pytest.raises(RuntimeError):claim_ledger(store,configuration(tmp_path/"old.sqlite"))
    assert store.get("paper_cash")==12345 and store.get("experiment") is None
    store.db.close()

@pytest.mark.parametrize("side",["LONG","SHORT"])
def test_sl_tp_are_three_atr_and_three_r_from_actual_fill(tmp_path,side):
    cfg=configuration(tmp_path/"fill.sqlite");store=Store(cfg.db);claim_ledger(store,cfg)
    broker=PaperBroker(cfg,store,NoAlerts())
    trade=fill_plan(side,100.,2.,10000.,cfg,dict(step=".001",tick=".001",min_qty=.001,max_qty=1000,min_notional=5))
    broker.enter("BTCUSDT",trade,900000,entered_at_ms=900001)
    p=broker.positions()[0]
    assert abs(p["entry"]-float(p["stop"]))==pytest.approx(6.,abs=.001)
    assert abs(p["entry"]-float(p["take"]))==pytest.approx(18.,abs=.001)
    assert p["risk"]<=25 and p["entry"]*float(p["quantity"])<=1000
    store.db.close()

def setup_book(tmp_path):
    c=configuration(tmp_path/"slow.sqlite");s=Store(c.db);claim_ledger(s,c)
    return c,s,PaperBroker(c,s,NoAlerts())

def test_valid_setup_uses_cap_and_cannot_replay_after_restart(tmp_path,monkeypatch):
    import bot.slow_paper as runner
    cfg,store,broker=setup_book(tmp_path)
    now=FOUR_HOURS*1000+1000
    monkeypatch.setattr(runner,"decide_slow",lambda f,blocked=False:Signal("NO_TRADE","blocked") if blocked else Signal("LONG","candidate"))
    status=cycle(cfg,store,None,broker,offline=True,now_ms=now)
    assert status["entries"]==2 and len(broker.positions())==2
    assert all(p["risk"]<=25 for p in broker.positions())
    again=cycle(cfg,store,None,broker,offline=True,now_ms=now+1000)
    assert again["entries"]==0
    store.db.close()
    reopened=Store(cfg.db);new_broker=PaperBroker(cfg,reopened,NoAlerts())
    assert cycle(cfg,reopened,None,new_broker,offline=True,now_ms=now+2000)["entries"]==0
    reopened.db.close()

def test_late_setup_is_consumed_and_never_enters(tmp_path,monkeypatch):
    import bot.slow_paper as runner
    cfg,store,broker=setup_book(tmp_path)
    monkeypatch.setattr(runner,"decide_slow",lambda *a,**kw:Signal("LONG","candidate"))
    status=cycle(cfg,store,None,broker,offline=True,now_ms=FOUR_HOURS*1000+900000)
    assert status["entries"]==0 and not broker.positions()
    assert store.get("last_slow_bar:BTCUSDT")==FOUR_HOURS*1000-1
    store.db.close()

def test_funding_failure_blocks_entries_while_exit_is_managed(tmp_path,monkeypatch):
    import bot.slow_paper as runner
    cfg,store,broker=setup_book(tmp_path);now=FOUR_HOURS*1000+1000
    trade=fill_plan("LONG",63600.,1000.,10000.,cfg,dict(step=".001",tick=".01",min_qty=.001,max_qty=1000,min_notional=5))
    broker.enter("BTCUSDT",trade,now-900001,entered_at_ms=now-960000)
    class Market:
        offset=0
        def interval_candles(self,symbol,interval):
            bars=runner.synthetic_bars(now,symbol,900000 if interval=="15m" else FOUR_HOURS)
            if symbol=="BTCUSDT" and interval=="15m":bars[-1].low=59000.
            return bars
        def call(self,*args):return {"markPrice":63600.}
        def funding_history(self,*args):raise RuntimeError("unavailable")
    monkeypatch.setattr(runner,"decide_slow",lambda f,blocked=False:Signal("NO_TRADE","blocked") if blocked else Signal("LONG","candidate"))
    result=cycle(cfg,store,Market(),broker,now_ms=now)
    assert result["entries"]==0 and not broker.positions()
    assert store.get("paper_funding_pending")
    assert store.db.execute("SELECT count(*) FROM events WHERE kind='EXIT'").fetchone()[0]==1
    store.db.close()

@pytest.mark.parametrize("case",json.loads((Path(__file__).parent/"fixtures/slow_reference.json").read_text())["cases"] if (Path(__file__).parent/"fixtures/slow_reference.json").exists() else [])
def test_signal_and_indicators_match_frozen_research(case):
    from bot.indicators import Candle
    bars=[Candle(*row) for row in case["bars"]]
    f=slow_features(bars)
    for key,value in case["expected_features"].items():assert math.isclose(f[key],value,rel_tol=1e-10,abs_tol=1e-9)
    assert decide_slow(f).side==case["expected_side"]

def test_quote_age_uses_clock_after_network_retrieval(tmp_path,monkeypatch):
    import bot.slow_paper as runner
    cfg,store,broker=setup_book(tmp_path)
    clock=[FOUR_HOURS*1000+1000]
    monkeypatch.setattr(runner.time,"time",lambda:clock[0]/1000)
    monkeypatch.setattr(runner,"decide_slow",lambda f,blocked=False:Signal("NO_TRADE","blocked") if blocked else Signal("LONG","candidate"))
    class Market:
        offset=0
        def interval_candles(self,symbol,interval):
            clock[0]+=2000
            return runner.synthetic_bars(clock[0],symbol,900000 if interval=="15m" else FOUR_HOURS)
        def call(self,method,path,params):
            clock[0]+=2000
            price={"BTCUSDT":63600.,"ETHUSDT":3180.,"SOLUSDT":159.}[params["symbol"]]
            return {"markPrice":price}
        def filters(self,symbol):return dict(step=".001",tick=".01",min_qty=.001,max_qty=1000,min_notional=5)
        def entry_quote(self,symbol):
            clock[0]+=3000
            price={"BTCUSDT":63600.,"ETHUSDT":3180.,"SOLUSDT":159.}[symbol]
            return dict(mark=price,bid=price,ask=price,mark_time=clock[0],book_time=clock[0])
    assert cycle(cfg,store,Market(),broker)["entries"]==2
    store.db.close()
