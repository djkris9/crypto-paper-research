import time


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
        self.alerts.send(f"PAPER {p['symbol']} {reason}; PnL {pnl:.2f} USD")

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


