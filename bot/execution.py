import time
from decimal import Decimal
from .strategy import rounded


class PaperBroker:
    def __init__(self, config, store, alerts):
        self.config,self.store,self.alerts = config,store,alerts
        if store.get("paper_cash") is None:
            store.set("paper_cash",config.paper_balance)
        self.prices = {}
        from .funding import initialize
        initialize(store)

    def positions(self):
        return self.store.get("paper_positions",[])

    def equity(self):
        cash = self.store.get("paper_cash")
        return cash+sum((self.prices.get(p["symbol"],p["entry"])-p["entry"])*float(p["quantity"])*(1 if p["side"] == "LONG" else -1) for p in self.positions())

    def mark(self, symbol, candles, current_price=None, funding=None, observed_at_ms=None):
        if not self.config.paper_funding:
            funding=[]
        observed_at_ms=int(time.time()*1000) if observed_at_ms is None else observed_at_ms
        self.prices[symbol] = current_price or candles[-1].close
        positions = self.positions()
        for p in list(positions):
            if p["symbol"] != symbol:
                continue
            bars = [c for c in candles if c.close_time > p["opened_after"]]
            if bars and bars[0].close_time-p["opened_after"] > 900000:
                self.store.halt("Paper position candle history gap; exit could have been missed")
                continue
            for c in bars:
                if funding is not None:
                    from .funding import settle
                    settle(self.store,p,funding,c.close_time)
                long = p["side"] == "LONG"
                stop,take = float(p["stop"]),float(p["take"])
                stopped = c.low <= stop if long else c.high >= stop
                taken = c.high >= take if long else c.low <= take
                partial=p["entry_time_ms"]>c.close_time-900000+1
                if partial and taken:
                    # A favorable high/low before entry must not create paper profit.
                    taken=False
                    self.store.log("PARTIAL_ENTRY_BAR",symbol,position_id=p["position_id"],candle_time=c.close_time)
                if stopped or taken:
                    # Stop first if both touched; gap-through stop fills at adverse open.
                    price = (min(stop,c.open) if long else max(stop,c.open)) if stopped else take
                    self._close(p,price, "SL" if stopped else "TP",positions,funding_until=c.close_time)
                    break
                p["opened_after"] = c.close_time
            if p in positions and funding is not None:
                from .funding import settle
                settle(self.store,p,funding,observed_at_ms)
            if p in positions and current_price is not None and observed_at_ms>=p["entry_time_ms"]:
                long=p["side"]=="LONG"
                stop,take=float(p["stop"]),float(p["take"])
                stopped=current_price<=stop if long else current_price>=stop
                taken=current_price>=take if long else current_price<=take
                if stopped or taken:
                    price=(min(stop,current_price) if long else max(stop,current_price)) if stopped else take
                    self._close(p,price,"SL" if stopped else "TP",positions,funding_until=observed_at_ms)
        self.store.set("paper_positions",positions)

    def _close(self,p,price,reason,positions,funding_until=None):
        qty = float(p["quantity"])
        fill = price*(.9998 if p["side"] == "LONG" else 1.0002)
        pnl = (fill-p["entry"])*qty*(1 if p["side"] == "LONG" else -1)-qty*fill*.0005
        # Cash and position update are atomic to survive a process interruption.
        positions.remove(p)
        with self.store.db:
            import json
            pending=self.store.get("paper_funding_pending",[])
            if self.config.paper_funding and funding_until is not None:
                pending.append(dict(p,funding_end_ms=funding_until,exit_fill=fill,exit_fee=qty*fill*.0005,exit_cash_delta=pnl))
            for key,value in (("paper_cash",self.store.get("paper_cash")+pnl),("paper_positions",positions),("paper_funding_pending",pending)):
                self.store.db.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,json.dumps(value)))
        funding_amount=self.store.db.execute("SELECT COALESCE(SUM(amount),0) FROM funding_payments WHERE position_id=?",(p["position_id"],)).fetchone()[0]
        self.store.log("EXIT",p["symbol"],reason=reason,pnl=pnl,fill=fill,position_id=p["position_id"],
                       entry_fee=p["entry_fee"],exit_fee=qty*fill*.0005,funding_applied=funding_amount,
                       net_trade_pnl=pnl-p["entry_fee"]+funding_amount,net_trade_pnl_provisional=self.config.paper_funding)
        self.alerts.send(f"PAPER {p['symbol']} {reason}; PnL {pnl:.2f} USDT")

    def enter(self, symbol, trade, candle_time, entered_at_ms=None):
        from uuid import uuid4
        p = dict(trade,symbol=symbol,opened_after=candle_time,
                 entry_time_ms=candle_time+1 if entered_at_ms is None else int(entered_at_ms),position_id=uuid4().hex)
        p["entry"] *= 1.0002 if p["side"] == "LONG" else .9998
        p["entry_fee"]=float(p["quantity"])*p["entry"]*.0005
        positions = self.positions()+[p]
        import json
        with self.store.db:
            for key,value in (("paper_cash",self.store.get("paper_cash")-p["entry_fee"]),("paper_positions",positions)):
                self.store.db.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,json.dumps(value)))
        self.store.log("ENTRY",symbol,trade=p)
        self.alerts.send(f"PAPER {symbol} {p['side']} qty={p['quantity']} SL={p['stop']} TP={p['take']}")


    def reconcile_funding(self, exchange, now_ms):
        from .funding import settle
        pending=self.store.get("paper_funding_pending",[])
        if not pending:return True
        remaining=[]
        finalized=[]
        for position in pending:
            # Recheck closed positions after a publication grace period.
            if now_ms < position["funding_end_ms"]+600000:
                remaining.append(position)
                continue
            records=exchange.funding_history(position["symbol"],position["entry_time_ms"]+1,position["funding_end_ms"])
            settle(self.store,position,records,position["funding_end_ms"])
            funding_amount=self.store.db.execute("SELECT COALESCE(SUM(amount),0) FROM funding_payments WHERE position_id=?",(position["position_id"],)).fetchone()[0]
            finalized.append((position["symbol"],dict(position_id=position["position_id"],
                funding=funding_amount,entry_fee=position["entry_fee"],exit_fee=position["exit_fee"],
                net_trade_pnl=position["exit_cash_delta"]-position["entry_fee"]+funding_amount)))
        import json
        from datetime import datetime,timezone
        with self.store.db:
            for symbol,payload in finalized:
                self.store.db.execute("INSERT INTO events(time,kind,symbol,payload) VALUES (?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(),"TRADE_RECONCILED",symbol,json.dumps(payload,allow_nan=False)))
            self.store.db.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("paper_funding_pending",json.dumps(remaining,allow_nan=False)))
        return not remaining


class DemoBroker:
    def __init__(self, config, store, alerts, exchange):
        self.config,self.store,self.alerts,self.exchange = config,store,alerts,exchange
        exchange.validate_mode()

    def positions(self):
        return list(self.store.get("demo_positions",{}).values())

    def equity(self):
        return self.exchange.account()[0]

    def reconcile(self):
        # Dedicated account: unexpected positions/orders halt, never get adopted.
        actual = {p["symbol"]:p for p in self.exchange.positions()}
        tracked = self.store.get("demo_positions",{})
        if self.store.get("pending_entry"):
            self.store.halt("Unresolved demo entry; reconcile on Binance before resuming")
        if set(actual)-set(tracked):
            self.store.halt("Untracked demo position detected")
        algos = self.exchange.open_algos()
        owned = {p[k] for p in tracked.values() for k in ("stop_id","take_id")}
        if any(a["algoId"] not in owned for a in algos):
            self.store.halt("Untracked conditional order detected")
        if self.exchange.call("GET","/fapi/v1/openOrders",signed=True):
            self.store.halt("Unexpected open regular order detected")
        for symbol,p in list(tracked.items()):
            if symbol not in actual:
                # Delete the sibling protection so it cannot affect a future position.
                for a in algos:
                    if a["algoId"] in {p["stop_id"],p["take_id"]}:
                        self.exchange.cancel_algo(a["algoId"])
                del tracked[symbol]
                self.store.log("DEMO_EXIT",symbol)
                self.alerts.send(f"DEMO {symbol} closed; remaining protection removed")
                continue
            actual_p = actual[symbol]
            expected_qty = Decimal(p["quantity"])*(1 if p["side"] == "LONG" else -1)
            valid = Decimal(actual_p["positionAmt"]) == expected_qty
            for kind,key,trigger in (("STOP_MARKET","stop_id","stop"),("TAKE_PROFIT_MARKET","take_id","take")):
                a = next((a for a in algos if a["algoId"] == p[key]),None)
                valid = valid and a is not None and a.get("orderType",a.get("type")) == kind and a.get("symbol") == symbol and a.get("side") == ("SELL" if p["side"] == "LONG" else "BUY") and str(a.get("closePosition")).lower() == "true" and a.get("workingType") == "MARK_PRICE" and Decimal(str(a.get("triggerPrice",0))) == Decimal(p[trigger])
            if not valid:
                self.store.halt("Position/protection mismatch; emergency demo close required")
                self._emergency_close(symbol)
        self.store.set("demo_positions",tracked)

    def _emergency_close(self,symbol):
        try:
            p = next((p for p in self.exchange.positions() if p["symbol"] == symbol),None)
            if p:
                amt = Decimal(p["positionAmt"])
                self.exchange.market(symbol,"SELL" if amt > 0 else "BUY",str(abs(amt)),"mvp-close-"+str(time.time_ns())[-18:],True)
                self.store.log("EMERGENCY_CLOSE_SENT",symbol)
        except Exception:
            self.store.log("EMERGENCY_CLOSE_UNCONFIRMED",symbol)
        self.alerts.send(f"DEMO SAFETY HALT {symbol}. Inspect positions and orders in Binance now.")

    def enter(self,symbol,trade,candle_time):
        x = self.exchange
        if self.store.get("halt") or self.store.get("pending_entry"):
            raise RuntimeError("Demo entry halted")
        _,available = x.account()
        if available < float(trade["quantity"])*trade["entry"]*1.05:
            raise ValueError("Insufficient available demo collateral")
        # One-way, isolated margin, 1x leverage. Use only an otherwise-empty demo account.
        try:
            x.call("POST","/fapi/v1/marginType",{"symbol":symbol,"marginType":"ISOLATED"},True)
        except RuntimeError:
            # Already-isolated returns an error; verify the actual mode before continuing.
            rows = x.call("GET","/fapi/v2/positionRisk",{"symbol":symbol},True)
            if not rows or any(row.get("marginType") != "isolated" for row in rows):
                raise
        leverage = x.call("POST","/fapi/v1/leverage",{"symbol":symbol,"leverage":1},True)
        if int(leverage["leverage"]) != 1:
            raise RuntimeError("Could not set 1x leverage")
        uid = "mvp-"+str(time.time_ns())[-18:]
        self.store.set("pending_entry",{"symbol":symbol,"client_id":uid,"trade":trade})
        try:
            order = x.market(symbol,"BUY" if trade["side"] == "LONG" else "SELL",trade["quantity"],uid)
            if order.get("status") != "FILLED" or Decimal(order["executedQty"]) != Decimal(trade["quantity"]):
                raise RuntimeError("Entry not fully filled")
            fill = float(order["avgPrice"])
            if abs(fill/trade["entry"]-1) > .001 or not (float(trade["stop"]) < fill < float(trade["take"]) if trade["side"] == "LONG" else float(trade["take"]) < fill < float(trade["stop"])):
                raise RuntimeError("Fill outside allowed slippage/protection range")
            exit_side = "SELL" if trade["side"] == "LONG" else "BUY"
            stop = x.protection(symbol,exit_side,trade["stop"],"STOP_MARKET",uid+"-sl")
            take = x.protection(symbol,exit_side,trade["take"],"TAKE_PROFIT_MARKET",uid+"-tp")
            p = dict(trade,symbol=symbol,entry=fill,stop_id=stop["algoId"],take_id=take["algoId"],opened_after=candle_time)
            tracked = self.store.get("demo_positions",{})
            tracked[symbol] = p
            self.store.set("demo_positions",tracked)
            self.store.set("pending_entry",None)
            self.reconcile()
            self.store.log("ENTRY",symbol,trade=p)
            self.alerts.send(f"DEMO {symbol} {p['side']} qty={p['quantity']} SL={p['stop']} TP={p['take']}")
        except Exception:
            self.store.halt("Demo entry/protection uncertain; inspect account before clearing halt")
            self._emergency_close(symbol)
            raise RuntimeError("Demo safety halt; inspect account") from None
