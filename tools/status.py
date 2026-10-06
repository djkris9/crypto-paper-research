"""Summarize simulated state without network access or exchange credentials."""
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from bot.slow_state import read_checkpoint
p=read_checkpoint(ROOT/"state/paper.json");s=p["state"]
closed=[e for e in p["events"] if e["kind"]=="TRADE_RECONCILED"]
print(json.dumps(dict(mode="paper",cash=s["paper_cash"],open_positions=len(s.get("paper_positions",[])),reconciled_trades=len(closed),reconciled_net_pnl=sum(e["payload"]["net_trade_pnl"] for e in closed),pending_funding=len(s.get("paper_funding_pending",[])),halt=s.get("halt")),indent=2))
