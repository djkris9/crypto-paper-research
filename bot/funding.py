"""Paper funding ledger: authoritative published rates, atomic cash and deduplication."""
import json
import math
from datetime import datetime,timezone

def initialize(store):
    prior={row[1] for row in store.db.execute("PRAGMA table_info(funding_payments)")}
    if prior and "rate_type" not in prior:
        raise RuntimeError("Old funding schema requires ledger review; use a fresh paper experiment")
    store.db.execute("""CREATE TABLE IF NOT EXISTS funding_payments (
        position_id TEXT, funding_time INTEGER, rate_type TEXT, symbol TEXT, rate REAL, mark REAL, quantity REAL, amount REAL,
        PRIMARY KEY(position_id,funding_time,rate_type))""")
    store.db.commit()

def settle(store, position, records, through_ms):
    eligible=[]
    for row in records:
        stamp=int(row["fundingTime"])
        rate=float(row["fundingRate"]);mark=float(row["markPrice"])
        rate_type=row.get("rateType","Regular")
        if rate_type not in {"Regular","Special"}:
            raise ValueError("Unknown funding rate type")
        if row.get("symbol")!=position["symbol"] or not math.isfinite(rate) or not math.isfinite(mark) or mark<=0:
            raise ValueError("Invalid funding record")
        if position["entry_time_ms"]<stamp<=through_ms:
            eligible.append((stamp,rate_type,rate,mark))
    quantity=float(position["quantity"])
    if not math.isfinite(quantity) or quantity<=0:
        raise ValueError("Invalid funding quantity")
    changed=0.
    with store.db:
        cash=store.get("paper_cash")
        for stamp,rate_type,rate,mark in sorted(eligible):
            amount=-quantity*mark*rate*(1 if position["side"]=="LONG" else -1)
            prior=store.db.execute("SELECT rate,mark,quantity FROM funding_payments WHERE position_id=? AND funding_time=? AND rate_type=?",
                                   (position["position_id"],stamp,rate_type)).fetchone()
            if prior:
                if tuple(prior)!=(rate,mark,quantity):raise ValueError("Funding record changed after settlement")
                continue
            store.db.execute("INSERT INTO funding_payments VALUES (?,?,?,?,?,?,?,?)",
                (position["position_id"],stamp,rate_type,position["symbol"],rate,mark,quantity,amount))
            payload=json.dumps(dict(position_id=position["position_id"],funding_time=stamp,rate_type=rate_type,rate=rate,mark=mark,quantity=quantity,amount=amount))
            store.db.execute("INSERT INTO events(time,kind,symbol,payload) VALUES (?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(),"FUNDING",position["symbol"],payload))
            cash+=amount;changed+=amount
        store.db.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         ("paper_cash",json.dumps(cash,allow_nan=False)))
    return changed
