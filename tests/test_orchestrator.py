"""
test_orchestrator.py — Exercises the live wiring the unit tests skip: the
override approval→execution seam, expiry/stop exits with real Black-Scholes
marks, the unsettled-aware equity, and the pending-modal de-dupe.

Runs offline with a dry-run broker. Run: python tests/test_orchestrator.py
"""

import datetime as dt
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

# Isolated DB before importing db.
_TMP = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_TMP, "trading.db")

import db                                                   # noqa: E402
from alpaca_exec import PaperBroker                         # noqa: E402
from options_math import bs_price, occ_symbol               # noqa: E402
from orchestrator import Orchestrator                       # noqa: E402
from signals import Signal                                  # noqa: E402
from strategies import OptionLeg, TradeCandidate            # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def base_config(**conv):
    cov = {"enabled": True, "max_allowed_tier": "MAX", "auto_execute_strong": False,
           "auto_execute_max": True, "all_in_requires_approval": True,
           "max_override_trades_per_day": 5, "max_override_trades_per_week": 5,
           "approval_timeout_seconds": 300, "log_rejected_overrides": True}
    cov.update(conv)
    return {
        "account": {"starting_cash": 10000.0, "account_type": "cash"},
        "risk": {"profile": "aggressive", "custom_overrides": {}},
        "conviction_overrides": cov,
        "trading": {"watchlist": ["XYZ"], "foreign_proxies": [],
                    "scan_interval_minutes": 5, "position_check_interval_seconds": 60,
                    "min_hold_minutes_options": 15, "zero_dte_force_close_et": "15:00",
                    "credit_trade_profit_target_pct": 0.5, "rationale_llm_enabled": False},
    }


def strong_candidate(symbol="XYZ"):
    """A candidate that scores 5/7 conviction criteria at strength 0.91 → STRONG."""
    exp = dt.date.today() + dt.timedelta(days=30)
    leg = OptionLeg("buy", "call", 100.0, exp, occ_symbol(symbol, exp, "call", 100))
    c = TradeCandidate(
        symbol=symbol, strategy="long_call", strategy_type="long_options",
        direction="bullish", legs=[leg], dte=30, expiry=exp,
        est_debit=4.0, est_credit=0.0, collateral_per_contract=400.0,
        max_profit=3000.0, max_loss=1000.0, signal_strength=0.91,
        signals=[Signal("oversold_in_uptrend", "technical", "bullish", 0.8),
                 Signal("institutional_call_flow", "options", "bullish", 0.85)],
        iv_rank=28.0, entry_iv=0.25,
        technical_direction="bullish", options_flow_direction="bullish",
        macro_direction="bullish", sentiment_direction="bullish",
        unusual_options_premium=2_000_000.0,
        catalyst=None,            # crit #4 false
        backtest_win_rate=None,   # crit #7 false  → exactly 5/7
    )
    return c


class Ctx:
    vix = 15.0
    fear_greed_index = 60.0


def make_orch(cfg):
    o = Orchestrator(cfg, broker=PaperBroker(dry_run=True))
    o.ctx.vix = 15.0
    o.ctx.fear_greed_index = 60.0
    return o


print("== Override approval seam ==")
o = make_orch(base_config())
cand = strong_candidate()
# STRONG with auto_execute_strong=False → should queue, not trade yet.
o.execute_candidate(cand, open_positions=[], deployed=0.0, equity=10000.0)
check("STRONG approval queued (no trade yet)", len(db.open_trades()) == 0)
check("pending execution context retained", len(o._pending_ovr) == 1)
approval_id = next(iter(o._pending_ovr))

before_cash = o.ledger.available()
res = o.execute_pending(approval_id, approved=True)
opens = db.open_trades()
check("approve actually submits a trade", res and res["executed"] and len(opens) == 1, res)
check("trade recorded with override tier", opens[0]["override_tier"] == "STRONG")
check("override sized above normal (1.5x)",
      (opens[0]["sized_dollars"] or 0) > (opens[0]["override_normal_size"] or 0))
check("ledger debited for the trade", o.ledger.available() < before_cash)
check("pending context cleared after execution", approval_id not in o._pending_ovr)

print("== Expiry on approval-skip → normal size ==")
o2 = make_orch(base_config())
o2.execute_candidate(strong_candidate("ABC"), [], 0.0, 10000.0)
aid = next(iter(o2._pending_ovr))
res2 = o2.execute_pending(aid, approved=False)
opens2 = db.open_trades()
abc = [t for t in opens2 if t["symbol"] == "ABC"]
check("skip/expiry still submits at normal size", res2 and res2["executed"] and len(abc) == 1)
check("skipped trade carries NO override tier", abc[0]["override_tier"] is None)

print("== Pending modal de-dupe (#11) ==")
o3 = make_orch(base_config())
o3.execute_candidate(strong_candidate("DUP"), [], 0.0, 10000.0)
o3.execute_candidate(strong_candidate("DUP"), [], 0.0, 10000.0)  # same symbol again
check("second scan does not stack a duplicate approval", len(o3._pending_ovr) == 1)

print("== Position marks drive exits ==")
o4 = make_orch(base_config())
today = dt.date.today()
exp = today + dt.timedelta(days=30)
leg = {"side": "buy", "kind": "call", "strike": 100.0, "expiry": exp.isoformat(),
       "symbol": occ_symbol("MARK", exp, "call", 100), "ratio": 1}
# Price the entry at fair BS value so a flat underlying ≈ breakeven.
entry_px = bs_price(100.0, 100.0, 30 / 365, 0.043, 0.25, "call")
cand_m = TradeCandidate(
    symbol="MARK", strategy="long_call", strategy_type="long_options",
    direction="bullish", legs=[OptionLeg("buy", "call", 100.0, exp, leg["symbol"])],
    dte=30, expiry=exp, est_debit=entry_px, collateral_per_contract=entry_px * 100,
    max_profit=entry_px * 300, max_loss=entry_px * 100, signal_strength=0.8,
    signals=[Signal("oversold_in_uptrend", "technical", "bullish", 0.8)],
    iv_rank=28.0, entry_iv=0.25)
tid = db.insert_trade(cand_m, 1, entry_px * 100, entry_px * 100, "aggressive",
                      "trending", "test", None)
# Age the position past the 15-min minimum hold so the stop is allowed to fire.
import orchestrator as _orc                                 # noqa: E402
o4._open_meta[tid] = {"opened_at": o4.now_et() - dt.timedelta(minutes=30),
                      "candidate": cand_m}
# Underlying collapses → long call loses most of its value → stop should fire.
o4.finnhub.quote = lambda s: {"c": 88.0}
o4.manage_positions()
check("collapsed long call hits the stop and closes",
      not any(t["symbol"] == "MARK" for t in db.open_trades()))

print("== Expiry sweep frees capital ==")
o5 = make_orch(base_config())
past_exp = today - dt.timedelta(days=1)
pleg = OptionLeg("buy", "call", 100.0, past_exp, occ_symbol("OLD", past_exp, "call", 100))
cand_e = TradeCandidate(
    symbol="OLD", strategy="long_call", strategy_type="long_options",
    direction="bullish", legs=[pleg], dte=0, expiry=past_exp,
    est_debit=5.0, collateral_per_contract=500.0, max_profit=1500.0, max_loss=500.0,
    signal_strength=0.8, signals=[Signal("x", "technical", "bullish", 0.8)],
    iv_rank=28.0, entry_iv=0.25)
db.insert_trade(cand_e, 1, 500.0, 500.0, "aggressive", "trending", "test", None)
o5.finnhub.quote = lambda s: {"c": 103.0}     # ITM by 3 at expiry
o5.manage_positions()
check("expired position is force-closed (no longer open)",
      not any(t["symbol"] == "OLD" for t in db.open_trades()))

print("== Unsettled-aware equity ==")
from risk import SettledCashLedger                          # noqa: E402
led = SettledCashLedger(settled=1000.0)
led.spend(600.0, today=dt.date(2026, 6, 8))
led.receive_proceeds(650.0, trade_date=dt.date(2026, 6, 8))
check("available() excludes unsettled proceeds",
      led.available(today=dt.date(2026, 6, 8)) == 400.0)
check("total() includes unsettled proceeds",
      led.total(today=dt.date(2026, 6, 8)) == 1050.0)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
