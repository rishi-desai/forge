"""
test_core.py — Offline validation of the risk/override/options math.
The spec's own worked examples (§5.4 table) are the test cases.

Run: python tests/test_core.py   (no pytest required)
"""

import datetime as dt
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import risk
from risk import (PROFILES, SettledCashLedger, InsufficientCashError, RiskRejection,
                  compute_position_size, validate_cash_account_order, get_profile)
from overrides import (OverrideManager, ApprovedOverride, OVERRIDE_TIERS,
                       determine_tier, evaluate_conviction_criteria)
import options_math as om

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def approx(a, b, tol=0.51):
    return abs(a - b) <= tol


print("== Sizing formula vs spec §5.4 worked examples ==")
P = PROFILES
check("$1k max_aggression 0.85 0dte → $364",
      approx(compute_position_size(1000, P["max_aggression"], 0.85, "0dte"), 364))
check("$1k max_aggression 0.50 long call → $280",
      approx(compute_position_size(1000, P["max_aggression"], 0.50, "long_options"), 280))
check("$10k moderate 0.80 spread → $704",
      approx(compute_position_size(10_000, P["moderate"], 0.80, "spread"), 704))
check("$10k aggressive 0.90 spread → $1,880",
      approx(compute_position_size(10_000, P["aggressive"], 0.90, "spread"), 1880))
check("$100k aggressive 0.90 spread → $8,000 (profile cap)",
      approx(compute_position_size(100_000, P["aggressive"], 0.90, "spread"), 8000))
check("$100k moderate 0.75 equity → $5,000 (profile cap, 1.2x equity scalar)",
      approx(compute_position_size(100_000, P["moderate"], 0.75, "equity"), 5000))
check("$100k conservative 0.80 spread → $2,640",
      approx(compute_position_size(100_000, P["conservative"], 0.80, "spread"), 2640))

print("== Floors, gates, universal cap ==")
try:
    compute_position_size(10_000, P["moderate"], 0.2, "spread")
    check("signal < 0.30 rejected", False)
except RiskRejection:
    check("signal < 0.30 rejected", True)
try:
    compute_position_size(10_000, P["conservative"], 0.9, "0dte")
    check("conservative blocks 0DTE", False)
except RiskRejection:
    check("conservative blocks 0DTE", True)
check("size never exceeds available cash (95% buffer)",
      compute_position_size(300, P["max_aggression"], 1.0, "long_options") <= 300 * 0.95 + 0.01)
huge = dict(P["aggressive"], max_position_pct=0.9, max_position_abs_cap=50_000)
check("universal $10k cap binds even with absurd custom overrides",
      compute_position_size(1_000_000, huge, 1.0, "spread") == 10_000)

print("== Override tiers ==")
check("0.91 + 5 criteria → STRONG", determine_tier(0.91, 5) == "STRONG")
check("0.96 + 6 criteria → MAX", determine_tier(0.96, 6) == "MAX")
check("0.99 + 7 criteria → ALL_IN", determine_tier(0.99, 7) == "ALL_IN")
check("0.97 + 5 criteria → STRONG (criteria gate MAX)", determine_tier(0.97, 5) == "STRONG")
check("0.89 + 7 criteria → None", determine_tier(0.89, 7) is None)

ov = ApprovedOverride("STRONG", {}, 5, normal_size=704)
check("STRONG 1.5x: $704 → $1,056 ($10k moderate)",
      approx(risk.compute_override_size(10_000, P["moderate"], ov), 1056))
ov = ApprovedOverride("MAX", {}, 6, normal_size=1880)
check("MAX 2.0x: $1,880 → $3,760 ($10k aggressive)",
      approx(risk.compute_override_size(10_000, P["aggressive"], ov), 3760))
ov = ApprovedOverride("STRONG", {}, 5, normal_size=8000)
got = risk.compute_override_size(100_000, P["aggressive"], ov)
check("spec contradiction resolved: $8k STRONG on $100k → $10,000 (universal cap), not $12,000",
      got == 10_000, f"got {got}")
ov = ApprovedOverride("ALL_IN", {}, 7, normal_size=5000)
got = risk.compute_override_size(100_000, P["moderate"], ov)
check("ALL_IN: min(50% cash, $10k universal) → $10,000 on $100k", got == 10_000, f"got {got}")
got = risk.compute_override_size(4_000, P["max_aggression"], ov)
check("ALL_IN on $4k account → $2,000 (50% of cash)", approx(got, 2000), f"got {got}")

print("== Override manager gating ==")
class Sig:  # minimal trade-signal stub
    symbol = "NVDA"; signal_strength = 0.99
    technical_direction = options_flow_direction = macro_direction = sentiment_direction = "bullish"
    unusual_options_premium = 2_100_000; max_profit = 6240; max_loss = 1880
    catalyst = "earnings in 18d"; strategy_type = "spread"; iv_rank = 28
    backtest_win_rate = 0.75; backtest_sample_size = 25
class Ctx:
    vix = 14.2; fear_greed_index = 61

crit, met = evaluate_conviction_criteria(Sig(), Ctx())
check("NVDA example: 7/7 criteria", met == 7, f"got {met}: {crit}")

mgr = OverrideManager({"enabled": True, "max_allowed_tier": "MAX",
                       "auto_execute_strong": True, "auto_execute_max": True,
                       "all_in_requires_approval": True,
                       "max_override_trades_per_day": 2, "log_rejected_overrides": True})
o = mgr.evaluate(Sig(), Ctx(), normal_size=1880)
check("ALL_IN-qualifying trade downgraded to user's MAX ceiling", o is not None and o.tier == "MAX")
check("downgraded MAX auto-executes (no approval needed)", o is not None and not o.requires_approval)
mgr.record_execution(o)
o2 = mgr.evaluate(Sig(), Ctx(), 1880)
check("MAX per-tier daily limit (1/day) blocks second", o2 is None)
check("blocked attempt logged", len(mgr.rejected_log) == 1)

mgr2 = OverrideManager({"enabled": False, "log_rejected_overrides": True})
check("master switch off → no override", mgr2.evaluate(Sig(), Ctx(), 1000) is None)

mgr3 = OverrideManager({"enabled": True, "max_allowed_tier": "ALL_IN",
                        "all_in_requires_approval": True,
                        "max_override_trades_per_day": 5})
o3 = mgr3.evaluate(Sig(), Ctx(), 1880, now=dt.datetime(2026, 6, 10, 10, 0))
check("ALL_IN requires approval and queues", o3.requires_approval and o3.approval_id in mgr3.pending)
check("pending override is not valid for sizing yet", not o3.is_valid())
expired = mgr3.expire_stale(now=dt.datetime(2026, 6, 10, 10, 6))
check("5-min timeout expires pending → fallback to normal", len(expired) == 1 and not expired[0].is_valid())

print("== Settled-cash ledger (cash account, T+1) ==")
led = SettledCashLedger(settled=1000)
mon = dt.date(2026, 6, 8)  # Monday
led.spend(600, today=mon)
led.receive_proceeds(650, trade_date=mon)
check("proceeds not spendable same day", led.available(today=mon) == 400)
check("proceeds settle next business day", led.available(today=dt.date(2026, 6, 9)) == 1050)
led2 = SettledCashLedger(settled=100)
led2.receive_proceeds(500, trade_date=dt.date(2026, 6, 12))  # Friday
check("Friday proceeds settle Monday", led2.available(today=dt.date(2026, 6, 15)) == 600)
try:
    led2.spend(10_000)
    check("overspend raises InsufficientCashError", False)
except InsufficientCashError:
    check("overspend raises InsufficientCashError", True)

print("== Cash-account order validation ==")
led3 = SettledCashLedger(settled=5000)
cost = validate_cash_account_order(
    {"strategy_type": "iron_condor", "contracts": 1, "spread_width": 5,
     "net_credit": 1.2}, led3)
check("iron condor reserves full width × 100 ($500), not net credit", cost == 500)
try:
    validate_cash_account_order(
        {"strategy_type": "cash_secured_put", "contracts": 1, "strike": 390}, led3)
    check("CSP needing $39,000 on $5,000 rejected", False)
except InsufficientCashError:
    check("CSP needing $39,000 on $5,000 rejected", True)
try:
    validate_cash_account_order(
        {"strategy_type": "long_options", "contracts": 1, "premium": 60,
         "is_naked_short": False}, SettledCashLedger(settled=50_000))
    check("$6,000 single-contract premium > $5k universal cap rejected", False)
except RiskRejection:
    check("$6,000 single-contract premium > $5k universal cap rejected", True)

print("== Guards ==")
g = risk.PortfolioGuards(P["moderate"])
g.start_day(10_000)
try:
    g.check_drawdown(9_400)  # -6% > 5% halt
    check("daily 5% drawdown halts moderate profile", False)
except RiskRejection:
    check("daily 5% drawdown halts moderate profile", True)
g2 = risk.PortfolioGuards(P["moderate"])
g2.start_day(10_000, week_start_equity=10_000)
g2.check_drawdown(9_650, now=dt.datetime(2026, 6, 10))  # Wed, -3.5% on week
check("weekly -3% by Wednesday halves sizes", g2.size_multiplier() == 0.5)
g3 = risk.PortfolioGuards(P["moderate"])
g3.start_day(10_000)
opens = [{"symbol": "AAPL"}, {"symbol": "MSFT"}, {"symbol": "NVDA"}]
try:
    g3.check_capacity(opens, 6500, 10_000, 800, "GOOGL")
    check("70% deployment cap enforced", False)
except RiskRejection:
    check("70% deployment cap enforced", True)
try:
    g3.check_capacity(opens, 2000, 10_000, 500, "AMD")  # 4th tech position
    check("correlation guard: 4th tech position blocked (limit 3)", False)
except RiskRejection:
    check("correlation guard: 4th tech position blocked (limit 3)", True)

print("== Options math ==")
c = om.bs_price(100, 100, 30 / 365, 0.043, 0.25, "call")
p = om.bs_price(100, 100, 30 / 365, 0.043, 0.25, "put")
parity = c - p - (100 - 100 * math.exp(-0.043 * 30 / 365))
check("put-call parity holds", abs(parity) < 1e-6, f"resid {parity:.2e}")
iv = om.implied_vol(c, 100, 100, 30 / 365, 0.043, "call")
check("IV solver recovers 0.25", abs(iv - 0.25) < 1e-3, f"got {iv}")
d = om.bs_greeks(100, 100, 30 / 365, 0.043, 0.25, "call")["delta"]
check("ATM call delta ≈ 0.5", 0.45 < d < 0.58, f"got {d:.3f}")
k = om.strike_for_delta(100, 30 / 365, 0.043, 0.25, "put", 0.25, 1.0)
check("25-delta put strike is OTM below spot", k < 100, f"got {k}")
sym = om.occ_symbol("AAPL", dt.date(2026, 6, 20), "call", 220)
check("OCC symbol AAPL260620C00220000", sym == "AAPL260620C00220000", sym)
rt = om.parse_occ_symbol(sym)
check("OCC round-trip", rt["strike"] == 220 and rt["expiry"] == dt.date(2026, 6, 20))
check("IV rank linear", om.iv_rank(0.30, [0.20, 0.40]) == 50.0)

print("== Profiles ==")
try:
    get_profile("yolo")
    check("unknown profile rejected", False)
except ValueError:
    check("unknown profile rejected", True)
pcust = get_profile("moderate", {"max_position_abs_cap": 2000})
check("custom_overrides applied", pcust["max_position_abs_cap"] == 2000)
try:
    get_profile("moderate", {"made_up_key": 1})
    check("unknown custom_override key rejected", False)
except ValueError:
    check("unknown custom_override key rejected", True)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
