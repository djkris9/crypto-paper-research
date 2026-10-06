"""Portable paper checkpoint, preserving cash, funding deduplication and safety latches."""
import json
import math
import os
from pathlib import Path
import re
import tempfile
from .funding import initialize
from .slow_paper import EXPERIMENT,SPEC_HASH

CRITICAL={"ENTRY","EXIT","TRADE_RECONCILED","FUNDING","HALT","PORTFOLIO_DRAWDOWN_HALT"}
DIAGNOSTIC={"DECISION","DATA_ERROR","CYCLE_ERROR","FUNDING_UNAVAILABLE","FUNDING_LEDGER_ERROR","VALUATION_UNAVAILABLE","PARTIAL_ENTRY_BAR","ENTRY_QUOTE"}
KEYS={"experiment","mode","strategy_spec_sha256","runtime_configuration","paper_cash","paper_positions","paper_funding_pending","equity_peak","portfolio_drawdown_halt","halt","last_halt_alert"}

def allowed_key(key):
    return key in KEYS or re.fullmatch(r"daily_(equity|block):\d{4}-\d{2}-\d{2}",key) is not None or re.fullmatch(r"last_slow_bar:(BTC|ETH|SOL)USDT",key) is not None

def check_json(value):
    if isinstance(value,float) and not math.isfinite(value):raise ValueError("Nonfinite checkpoint value")
    if isinstance(value,dict):
        for key,item in value.items():
            if not isinstance(key,str) or any(term in key.lower() for term in ("password","api_key","secret","token")):raise ValueError("Credential fields are forbidden in public paper state")
            check_json(item)
    elif isinstance(value,list):
        for item in value:check_json(item)

def validate(payload):
    check_json(payload)
    if payload.get("schema")!=1 or payload.get("experiment")!=EXPERIMENT or payload.get("strategy_spec_sha256")!=SPEC_HASH:raise ValueError("Wrong checkpoint experiment/schema")
    state=payload["state"]
    if not all(allowed_key(k) for k in state) or state.get("mode")!="paper" or state.get("experiment")!=EXPERIMENT or state.get("strategy_spec_sha256")!=SPEC_HASH:raise ValueError("Invalid checkpoint state")
    if not isinstance(state.get("paper_cash"),(int,float)) or isinstance(state["paper_cash"],bool):raise ValueError("Missing paper cash")
    for key in ("paper_positions","paper_funding_pending"):
        if not isinstance(state.get(key,[]),list):raise ValueError("Invalid position list")
        for position in state.get(key,[]):
            if position.get("symbol") not in {"BTCUSDT","ETHUSDT","SOLUSDT"} or position.get("side") not in {"LONG","SHORT"}:raise ValueError("Invalid position")
            for field in ("entry","quantity","stop","take","risk"):
                if not math.isfinite(float(position[field])) or float(position[field])<=0:raise ValueError("Invalid position inputs")
    for event in payload["events"]:
        if event["kind"] not in CRITICAL|DIAGNOSTIC:raise ValueError("Unknown event type")
    for row in payload["funding_payments"]:
        if len(row)!=8 or row[2] not in {"Regular","Special"} or row[3] not in {"BTCUSDT","ETHUSDT","SOLUSDT"}:raise ValueError("Invalid funding checkpoint record")
        if not isinstance(row[1],int) or row[1]<0 or not all(isinstance(x,(int,float)) and math.isfinite(x) for x in row[4:]):raise ValueError("Invalid funding checkpoint numbers")
        if row[5]<=0 or row[6]<=0:raise ValueError("Invalid funding mark/quantity")
    if len(payload["funding_payments"])>100000:raise ValueError("Checkpoint exceeds research bounds")

def read_checkpoint(path):
    path=Path(path)
    if not path.is_file():raise RuntimeError("Forward checkpoint missing; refuse to reset experiment")
    if path.stat().st_size>10000000:raise RuntimeError("Checkpoint exceeds10MB; archive and review before continuing")
    payload=json.loads(path.read_text(encoding="utf-8"),parse_constant=lambda x:(_ for _ in ()).throw(ValueError("Nonfinite JSON")))
    validate(payload)
    return payload

def restore_checkpoint(store,path):
    payload=read_checkpoint(path)
    if store.db.execute("SELECT count(*) FROM state").fetchone()[0] or store.db.execute("SELECT count(*) FROM events").fetchone()[0]:raise RuntimeError("Restore requires an empty ephemeral DB; persistent services do not use --checkpoint")
    initialize(store)
    with store.db:
        for key,value in payload["state"].items():store.db.execute("INSERT INTO state VALUES (?,?)",(key,json.dumps(value,allow_nan=False)))
        for event in payload["events"]:
            store.db.execute("INSERT INTO events VALUES (?,?,?,?,?)",(event["id"],event["time"],event["kind"],event["symbol"],json.dumps(event["payload"],allow_nan=False)))
        for row in payload["funding_payments"]:store.db.execute("INSERT INTO funding_payments VALUES (?,?,?,?,?,?,?,?)",row)

def checkpoint_payload(store):
    state={key:json.loads(value) for key,value in store.db.execute("SELECT key,value FROM state")}
    if "runtime_configuration" in state:state["runtime_configuration"]={k:v for k,v in state["runtime_configuration"].items() if k!="db"}
    rows=store.db.execute("SELECT id,time,kind,symbol,payload FROM events ORDER BY id").fetchall()
    diagnostic=[row[0] for row in rows if row[2] in DIAGNOSTIC][-300:]
    keep=set(diagnostic)
    events=[dict(id=r[0],time=r[1],kind=r[2],symbol=r[3],payload=json.loads(r[4])) for r in rows if r[2] in CRITICAL or r[0] in keep]
    payments=[list(row) for row in store.db.execute("SELECT * FROM funding_payments ORDER BY position_id,funding_time,rate_type")]
    payload=dict(schema=1,experiment=EXPERIMENT,strategy_spec_sha256=SPEC_HASH,state=state,events=events,funding_payments=payments)
    validate(payload)
    return payload

def write_atomic(path,payload):
    validate(payload)
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    text=json.dumps(payload,indent=2,sort_keys=True,allow_nan=False)+"\n"
    if path.exists() and path.read_text(encoding="utf-8")==text:return False
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=path.parent,suffix=".tmp",delete=False) as f:
            temporary=Path(f.name);f.write(text);f.flush();os.fsync(f.fileno())
        os.replace(temporary,path)
    finally:
        if temporary and temporary.exists():temporary.unlink()
    return True

def export_checkpoint(store,path):return write_atomic(path,checkpoint_payload(store))

def initial_checkpoint():
    return dict(schema=1,experiment=EXPERIMENT,strategy_spec_sha256=SPEC_HASH,state=dict(experiment=EXPERIMENT,mode="paper",strategy_spec_sha256=SPEC_HASH,paper_cash=10000.,paper_positions=[],paper_funding_pending=[]),events=[],funding_payments=[])
