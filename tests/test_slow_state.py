import json
import sqlite3
import pytest
from bot.store import Store
from bot.execution import PaperBroker
from bot.funding import settle
from bot.strategy import portfolio_guard
from bot.slow_paper import configuration,claim_ledger,NoAlerts
from bot.slow_state import initial_checkpoint,write_atomic,restore_checkpoint,export_checkpoint,read_checkpoint

def fresh(tmp_path,name):
    c=configuration(tmp_path/name);s=Store(c.db);claim_ledger(s,c)
    return s,PaperBroker(c,s,NoAlerts())

def test_checkpoint_preserves_funding_deduplication_and_cash(tmp_path):
    s,b=fresh(tmp_path,"a.sqlite")
    b.enter("BTCUSDT",dict(side="LONG",quantity="1",entry=100.,stop="94",take="118",risk=6.2),900000,entered_at_ms=900001)
    position=b.positions()[0]
    record=dict(symbol="BTCUSDT",fundingTime=1000000,fundingRate=".001",markPrice="100")
    settle(s,position,[record],1000000);cash=s.get("paper_cash")
    checkpoint=tmp_path/"state.json";export_checkpoint(s,checkpoint)
    restored=Store(tmp_path/"b.sqlite");restore_checkpoint(restored,checkpoint)
    assert settle(restored,restored.get("paper_positions")[0],[record],1000000)==0
    assert restored.get("paper_cash")==cash and restored.db.execute("SELECT count(*) FROM funding_payments").fetchone()[0]==1
    assert export_checkpoint(restored,checkpoint) is False
    s.db.close();restored.db.close()

def test_drawdown_halt_survives_checkpoint_and_new_day(tmp_path):
    s,b=fresh(tmp_path,"guard.sqlite")
    portfolio_guard(s,10000,.05);s.set("paper_cash",9400.);portfolio_guard(s,9400,.05)
    path=tmp_path/"state.json";export_checkpoint(s,path)
    new=Store(tmp_path/"restored.sqlite");restore_checkpoint(new,path)
    portfolio_guard(new,11000,.05)
    assert new.get("portfolio_drawdown_halt") and new.get("halt")
    s.db.close();new.db.close()

def test_missing_or_nonfinite_checkpoint_cannot_reset_trial(tmp_path):
    with pytest.raises(RuntimeError):read_checkpoint(tmp_path/"missing.json")
    path=tmp_path/"bad.json";path.write_text('{"schema":NaN}')
    with pytest.raises(ValueError):read_checkpoint(path)

def test_secret_field_cannot_replace_public_checkpoint(tmp_path):
    path=tmp_path/"state.json";payload=initial_checkpoint();write_atomic(path,payload);prior=path.read_bytes()
    payload["state"]["api_key"]="test-placeholder"
    with pytest.raises(ValueError):write_atomic(path,payload)
    assert path.read_bytes()==prior

def test_corrupt_restore_rolls_back_every_state_row(tmp_path):
    payload=initial_checkpoint();event=dict(id=1,time="2026-10-06",kind="HALT",symbol="",payload={"reason":"test"})
    payload["events"]=[event,event]
    path=tmp_path/"duplicate.json";write_atomic(path,payload)
    store=Store(tmp_path/"empty.sqlite")
    with pytest.raises(sqlite3.IntegrityError):restore_checkpoint(store,path)
    assert store.db.execute("SELECT count(*) FROM state").fetchone()[0]==0
    assert store.db.execute("SELECT count(*) FROM events").fetchone()[0]==0
    store.db.close()
