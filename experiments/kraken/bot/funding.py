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
    """Accrue each published hourly USD/unit rate for actual time held.

    funding_time identifies an hour's START, rate is USD/base-unit/hour,
    mark is the unit conversion 1, and amount is cumulative for that hour.
    Repeated/older observations cannot charge the same duration twice.
    """
    hour = 3600000
    entry = int(position["entry_time_ms"])
    through = min(int(through_ms), int(position.get("funding_end_ms", through_ms)))
    quantity = float(position["quantity"])
    if not math.isfinite(quantity) or quantity <= 0 or position["side"] not in {"LONG", "SHORT"}:
        raise ValueError("Invalid Kraken funding position")
    if through <= entry:
        return 0.0
    rates = {}
    for row in records:
        stamp = int(row["hour_start_ms"])
        rate = float(row["hourly_usd_per_unit"])
        if stamp < 0 or stamp % hour or row["symbol"] != position["symbol"] or not math.isfinite(rate):
            raise ValueError("Invalid hourly funding record")
        if stamp in rates and rates[stamp] != rate:
            raise ValueError("Conflicting hourly funding rate")
        rates[stamp] = rate
    expected = set(range(entry // hour * hour, (through - 1) // hour * hour + hour, hour))
    if not expected.issubset(rates):
        raise ValueError("Missing hourly funding coverage")
    sign = 1 if position["side"] == "LONG" else -1
    changed = 0.0
    with store.db:
        cash = store.get("paper_cash")
        for stamp in sorted(expected):
            rate = rates[stamp]
            held_ms = min(through, stamp + hour) - max(entry, stamp)
            target = -quantity * rate * sign * held_ms / hour
            prior = store.db.execute("SELECT rate,mark,quantity,amount FROM funding_payments WHERE position_id=? AND funding_time=? AND rate_type=?",
                (position["position_id"], stamp, "Regular")).fetchone()
            if prior and tuple(prior[:3]) != (rate, 1.0, quantity):
                raise ValueError("Published hourly funding rate changed")
            old = prior[3] if prior else 0.0
            if prior and abs(target) <= abs(old):
                continue
            delta = target - old
            store.db.execute("INSERT INTO funding_payments VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(position_id,funding_time,rate_type) DO UPDATE SET amount=excluded.amount",
                (position["position_id"], stamp, "Regular", position["symbol"], rate, 1.0, quantity, target))
            if delta:
                payload = json.dumps(dict(position_id=position["position_id"], hour_start_ms=stamp,
                    hourly_usd_per_unit=rate, quantity=quantity, held_ms=held_ms, amount=delta,
                    cumulative_hour_amount=target, currency="USD"), allow_nan=False)
                store.db.execute("INSERT INTO events(time,kind,symbol,payload) VALUES (?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(), "FUNDING", position["symbol"], payload))
            cash += delta
            changed += delta
        store.db.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("paper_cash", json.dumps(cash, allow_nan=False)))
    return changed

