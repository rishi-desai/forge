"""
alpaca_exec.py — Layer 4 execution against the Alpaca Paper Trading API.

Cash-account semantics are enforced in OUR risk layer regardless of how the
Alpaca paper account is configured: even if the paper account reports margin
buying power, this module only ever spends what risk.SettledCashLedger says is
settled. That makes behavior translate 1:1 to a real cash account.

Multi-leg orders use Alpaca's MLEG order class (options level 3 required —
check your paper account's options_approved_level; raise it in the dashboard
if spreads are rejected).
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Optional

from risk import (InsufficientCashError, SettledCashLedger,
                  validate_cash_account_order)

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import (GetOrdersRequest, LimitOrderRequest,
                                         MarketOrderRequest, OptionLegRequest)
    from alpaca.trading.enums import (OrderClass, OrderSide, OrderStatus,
                                      PositionIntent, TimeInForce)
    ALPACA_AVAILABLE = True
except ImportError:  # codebase stays importable for tests / dashboard-only mode
    ALPACA_AVAILABLE = False


class PaperBroker:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run or not ALPACA_AVAILABLE
        if not self.dry_run:
            self.client = TradingClient(
                api_key=os.environ["ALPACA_API_KEY"],
                secret_key=os.environ["ALPACA_SECRET_KEY"],
                paper=True,
            )
        self._dry_positions: list[dict] = []
        self._dry_cash: float = 0.0

    # ------------------------------------------------------------------ account

    def account_snapshot(self) -> dict:
        if self.dry_run:
            return {"cash": self._dry_cash, "equity": self._dry_cash,
                    "options_level": 3, "source": "dry_run"}
        a = self.client.get_account()
        return {
            "cash": float(a.cash),
            "equity": float(a.equity),
            "buying_power": float(a.buying_power),
            "options_level": getattr(a, "options_approved_level", None),
            "source": "alpaca_paper",
        }

    def sync_ledger(self, ledger: SettledCashLedger) -> None:
        """Reconcile our settled-cash ledger against the broker's view; never
        let our 'settled' figure exceed what Alpaca reports as cash."""
        snap = self.account_snapshot()
        if snap["cash"] < ledger.available():
            ledger.settled = snap["cash"]

    def positions(self) -> list[dict]:
        if self.dry_run:
            return self._dry_positions
        out = []
        for p in self.client.get_all_positions():
            out.append({
                "symbol": p.symbol,
                "qty": float(p.qty),
                "side": str(p.side),
                "avg_entry": float(p.avg_entry_price),
                "current": float(p.current_price or 0),
                "unrealized_pl": float(p.unrealized_pl or 0),
                "asset_class": str(p.asset_class),
            })
        return out

    # ------------------------------------------------------------------ orders

    def submit_candidate(self, cand, contracts: int, ledger: SettledCashLedger) -> dict:
        """Validate against cash rules then submit. Returns an order record dict.
        Raises InsufficientCashError / RiskRejection on violation."""
        order_desc = self._describe(cand, contracts)
        cost = validate_cash_account_order(order_desc, ledger)

        if self.dry_run:
            ledger.spend(cost)
            rec = {"id": f"dry-{dt.datetime.now().timestamp():.0f}", "status": "filled",
                   "cost": cost, **order_desc}
            self._dry_positions.append({"symbol": cand.symbol, "candidate": cand,
                                        "qty": contracts, "cost": cost})
            return rec

        if cand.strategy_type == "equity":
            req = MarketOrderRequest(symbol=cand.symbol, qty=contracts,
                                     side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        elif len(cand.legs) == 1:
            leg = cand.legs[0]
            side = OrderSide.BUY if leg.side == "buy" else OrderSide.SELL
            limit = round(cand.est_debit if leg.side == "buy" else cand.est_credit, 2)
            req = LimitOrderRequest(symbol=leg.symbol, qty=contracts, side=side,
                                    limit_price=max(limit, 0.01),
                                    time_in_force=TimeInForce.DAY)
        else:
            legs = [OptionLegRequest(
                        symbol=l.symbol, ratio_qty=l.ratio,
                        side=OrderSide.BUY if l.side == "buy" else OrderSide.SELL,
                        position_intent=(PositionIntent.BUY_TO_OPEN if l.side == "buy"
                                         else PositionIntent.SELL_TO_OPEN))
                    for l in cand.legs]
            net = cand.est_debit if cand.est_debit > 0 else -cand.est_credit
            req = LimitOrderRequest(qty=contracts, order_class=OrderClass.MLEG,
                                    legs=legs, limit_price=round(net, 2),
                                    time_in_force=TimeInForce.DAY)
        o = self.client.submit_order(order_data=req)
        ledger.spend(cost)  # reserve immediately; refined on fill reconciliation
        return {"id": str(o.id), "status": str(o.status), "cost": cost, **order_desc}

    def close_position(self, symbol: str, ledger: SettledCashLedger,
                       proceeds_estimate: float = 0.0) -> dict:
        if self.dry_run:
            self._dry_positions = [p for p in self._dry_positions if p["symbol"] != symbol]
            ledger.receive_proceeds(proceeds_estimate)
            return {"symbol": symbol, "status": "closed_dry"}
        res = self.client.close_position(symbol)
        ledger.receive_proceeds(proceeds_estimate)  # settles T+1
        return {"symbol": symbol, "status": "close_submitted", "order": str(res)}

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _describe(cand, contracts: int) -> dict:
        d = {"symbol": cand.symbol, "strategy": cand.strategy, "contracts": contracts}
        st = cand.strategy
        if cand.strategy_type == "equity":
            d.update(strategy_type="equity", shares=contracts, price=cand.collateral_per_contract)
        elif st == "cash_secured_put":
            d.update(strategy_type="cash_secured_put", strike=cand.legs[0].strike)
        elif st == "covered_call":
            d.update(strategy_type="covered_call")
        elif cand.is_credit:
            d.update(strategy_type="iron_condor", spread_width=cand.spread_width,
                     net_credit=cand.est_credit)
        elif len(cand.legs) > 1:
            d.update(strategy_type="debit_spread", net_debit=cand.est_debit)
        else:
            d.update(strategy_type="long_options", premium=cand.est_debit)
        return d
