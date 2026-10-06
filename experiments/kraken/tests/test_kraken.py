from datetime import datetime, timezone
import json
import pytest
from bot.kraken import KrakenPublic, HOUR, SYMBOLS
from bot.funding import settle
from bot.slow_paper import configuration, claim_ledger, NoAlerts, EXPERIMENT, SPEC_HASH
from bot.execution import PaperBroker
from bot.store import Store
from bot.slow_state import validate, initial_checkpoint, export_checkpoint, restore_checkpoint

def book(tmp_path, side="LONG", entry=HOUR//2):
    config=configuration(tmp_path/"kraken.sqlite")
    store=Store(config.db);claim_ledger(store,config)
    broker=PaperBroker(config,store,NoAlerts())
    broker.enter("BTCUSD",dict(side=side,quantity="2",entry=100.,stop="94",take="118",risk=12.),entry-1,entered_at_ms=entry)
    return store,broker,broker.positions()[0]

@pytest.mark.parametrize("method,path,params,signed",[
    ("POST","/derivatives/api/v3/sendorder",{},False),
    ("GET","/derivatives/api/v3/accounts",{},False),
    ("GET","/derivatives/api/v3/tickers",{},True),
    ("GET","/derivatives/api/v3/tickers",{"api_key":"test"},False),
    ("GET","/derivatives/api/v3/orderbook",{"symbol":"PI_XBTUSD"},False),
    ("GET","/api/charts/v1/trade/PF_XBTUSD/4h",{"count":100},False),
])
def test_gateway_rejects_private_mutations_credentials_and_other_contracts(method,path,params,signed):
    with pytest.raises((RuntimeError,ValueError)):
        KrakenPublic().call(method,path,params,signed)

def test_gateway_uses_fixed_host_no_auth_no_proxy_and_no_redirect(monkeypatch):
    market=KrakenPublic();seen={}
    class Response:
        status_code=200
        def json(self):return {"result":"success","tickers":[]}
    def get(url,**kwargs):seen.update(url=url,**kwargs);return Response()
    monkeypatch.setattr(market.session,"get",get)
    market.call("GET","/derivatives/api/v3/tickers")
    assert seen["url"]=="https://futures.kraken.com/derivatives/api/v3/tickers"
    assert seen["allow_redirects"] is False and not market.session.trust_env
    assert "headers" not in seen

def test_contract_precision_remains_base_units(monkeypatch):
    market=KrakenPublic()
    monkeypatch.setattr(market,"call",lambda *a,**kw:{"instruments":[dict(symbol="PF_XBTUSD",tradeable=True,type="flexible_futures",quote="USD",contractSize=1,contractValueTradePrecision=4,tickSize=1,maxPositionSize=1200)]})
    filters=market.filters("BTCUSD")
    assert filters["step"]=="0.0001" and filters["min_qty"]==.0001 and filters["tick"]=="1.0"

def test_unfinished_candle_not_used_and_missing_history_rejected(monkeypatch):
    import bot.kraken as module
    market=KrakenPublic();interval=14400000;now=202*interval+1000
    monkeypatch.setattr(module.time,"time",lambda:now/1000)
    rows=[dict(time=i*interval,open="100",high="101",low="99",close="100",volume="10") for i in range(1,203)]
    monkeypatch.setattr(market,"call",lambda *a,**kw:{"candles":rows})
    bars=market.interval_candles("BTCUSD","4h")
    assert len(bars)==201 and bars[-1].close_time==202*interval-1
    rows.pop(4)
    with pytest.raises(ValueError):market.interval_candles("BTCUSD","4h")

def test_funding_coverage_includes_partial_entry_hour_and_detects_missing(monkeypatch):
    market=KrakenPublic()
    rows=[dict(timestamp="1970-01-01T00:00:00Z",fundingRate=3.),dict(timestamp="1970-01-01T01:00:00Z",fundingRate=-2.)]
    monkeypatch.setattr(market,"call",lambda *a,**kw:{"rates":rows})
    rates=market.funding_history("BTCUSD",HOUR//2+1,HOUR+HOUR//2)
    assert [r["hour_start_ms"] for r in rates]==[0,HOUR]
    rows.pop(0)
    with pytest.raises(ValueError):market.funding_history("BTCUSD",HOUR//2+1,HOUR+HOUR//2)

@pytest.mark.parametrize("side,sign",[("LONG",-1),("SHORT",1)])
def test_continuous_funding_prorates_entry_exit_and_restart(tmp_path,side,sign):
    store,broker,position=book(tmp_path,side)
    rates=[dict(symbol="BTCUSD",hour_start_ms=0,hourly_usd_per_unit=3.),dict(symbol="BTCUSD",hour_start_ms=HOUR,hourly_usd_per_unit=-2.)]
    cash=store.get("paper_cash")
    assert settle(store,position,rates,HOUR)==pytest.approx(sign*3.) # 2units x halfhour x $3
    assert settle(store,position,rates,HOUR)==0
    assert settle(store,position,rates,HOUR//2+1000)==0 # old observations never reverse accrual
    checkpoint=tmp_path/"state.json";export_checkpoint(store,checkpoint)
    restored=Store(tmp_path/"restored.sqlite");restore_checkpoint(restored,checkpoint)
    p=restored.get("paper_positions")[0]
    assert settle(restored,p,rates,HOUR+HOUR//2)==pytest.approx(sign*-2.)
    assert restored.get("paper_cash")==pytest.approx(cash+sign)
    assert sum(row[0] for row in restored.db.execute("SELECT amount FROM funding_payments"))==pytest.approx(sign)
    store.db.close();restored.db.close()

def test_conflicting_funding_rolls_back_earlier_updates(tmp_path):
    store,_,position=book(tmp_path)
    rates=[dict(symbol="BTCUSD",hour_start_ms=0,hourly_usd_per_unit=3.),dict(symbol="BTCUSD",hour_start_ms=HOUR,hourly_usd_per_unit=2.)]
    settle(store,position,rates,HOUR+1000)
    cash=store.get("paper_cash")
    events=store.db.execute("SELECT count(*) FROM events").fetchone()[0]
    rates[1]["hourly_usd_per_unit"]=5.
    with pytest.raises(ValueError):settle(store,position,rates,2*HOUR)
    assert store.get("paper_cash")==cash and store.db.execute("SELECT count(*) FROM events").fetchone()[0]==events
    store.db.close()

def test_funding_missing_hour_cannot_modify_cash(tmp_path):
    store,_,position=book(tmp_path);cash=store.get("paper_cash")
    with pytest.raises(ValueError):settle(store,position,[],HOUR)
    assert store.get("paper_cash")==cash
    store.db.close()

def test_checkpoint_refuses_binance_experiment():
    state=initial_checkpoint();state["experiment"]="slow-breakout-4h-v1"
    with pytest.raises(ValueError):validate(state)

def test_stale_ticker_rejected(monkeypatch):
    import bot.kraken as module
    market=KrakenPublic();monkeypatch.setattr(module.time,"time",lambda:100.)
    monkeypatch.setattr(market,"call",lambda *a,**kw:{"serverTime":"1970-01-01T00:00:00Z","tickers":[dict(symbol="PF_XBTUSD",suspended=False,markPrice=100)]})
    with pytest.raises(ValueError):market.mark_price("BTCUSD")
