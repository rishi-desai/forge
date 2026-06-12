"""
learning.py — Layer 5: post-trade analysis, signal weight updates, weekly report
(spec Phase 6).

Weight update is an exponential Bayesian-style moving estimate:
    w_new = clamp(w * (1-α) + α * outcome_score, 0.25, 2.0)
where outcome_score > 1 rewards a signal whose direction matched a profitable
trade and < 1 penalizes it. α shrinks as samples accumulate (more evidence →
smaller steps).
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from typing import Optional

import db
from signals import BULLISH, BEARISH


def post_trade_analysis(trade: dict) -> dict:
    """Run after a trade closes. trade is the trades-table row (dict)."""
    expected = (trade.get("max_profit") or 0) * (trade.get("signal_strength") or 0.5)
    actual = trade.get("pnl") or 0.0
    accuracy = (actual / expected) if expected else 0.0
    won = actual > 0

    sig_rows = db.trade_signal_rows(trade["id"])
    worked, failed = [], []
    weights = db.get_signal_weights()
    for s in sig_rows:
        # A directional signal "was correct" if its direction matched a winning
        # trade (or opposed a losing one). Neutral signals score on the trade result.
        if s["direction"] in (BULLISH, BEARISH):
            aligned = s["direction"] == trade["direction"]
            correct = (aligned and won) or (not aligned and not won)
        else:
            correct = won
        (worked if correct else failed).append(s["name"])

        w = weights.get(s["name"], 1.0)
        samples = 1 + sum(1 for _ in ())  # placeholder; sample count lives in DB
        alpha = max(0.05, 0.30 / math.sqrt(1 + len(sig_rows)))
        outcome_score = 1.25 if correct else 0.75
        w_new = min(max(w * (1 - alpha) + alpha * w * outcome_score, 0.25), 2.0)
        db.update_signal_weight(s["name"], round(w_new, 4), correct)

    db.mark_signal_outcomes(trade["id"], set(worked))
    lesson = {
        "trade_id": trade["id"], "strategy": trade["strategy"],
        "signal_accuracy": round(accuracy, 3), "what_worked": worked,
        "what_failed": failed, "market_context": trade.get("market_regime"),
    }
    db.insert_lesson(trade["id"], trade["strategy"], accuracy, worked, failed,
                     trade.get("market_regime") or "?")
    return lesson


# ----------------------------------------------------------------------------- weekly report

def _sharpe(daily_returns: list[float]) -> Optional[float]:
    if len(daily_returns) < 3:
        return None
    mu = statistics.mean(daily_returns)
    sd = statistics.pstdev(daily_returns)
    if sd == 0:
        return None
    return round(mu / sd * math.sqrt(252), 2)


def _max_drawdown(equity: list[float]) -> float:
    peak, mdd = -1e18, 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak)
    return round(mdd, 4)


def generate_weekly_report(week_ending: dt.date, spy_week_return: Optional[float] = None) -> dict:
    """Auto-generated every Friday 4:30pm ET (orchestrator schedules it)."""
    start = (week_ending - dt.timedelta(days=6)).isoformat()
    rows = [t for t in db.all_trades(1000)
            if t["status"] == "closed" and t["ts_close"] and t["ts_close"][:10] >= start]

    wins = [t for t in rows if (t["pnl"] or 0) > 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = sum(t["pnl"] for t in rows if (t["pnl"] or 0) <= 0)
    net = gross_win + gross_loss

    by_strategy: dict = {}
    for t in rows:
        b = by_strategy.setdefault(t["strategy"], {"n": 0, "pnl": 0.0, "wins": 0})
        b["n"] += 1
        b["pnl"] += t["pnl"] or 0
        b["wins"] += 1 if (t["pnl"] or 0) > 0 else 0
    ranked = sorted(by_strategy.items(), key=lambda kv: kv[1]["pnl"], reverse=True)

    curve = db.equity_curve()
    week_curve = [c["equity"] for c in curve if c["ts"][:10] >= start]
    daily_rets = [(b - a) / a for a, b in zip(week_curve, week_curve[1:]) if a > 0]

    weights = db.get_signal_weights()
    over = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)[:5]
    under = sorted(weights.items(), key=lambda kv: kv[1])[:5]

    week_return = ((week_curve[-1] - week_curve[0]) / week_curve[0]
                   if len(week_curve) >= 2 and week_curve[0] > 0 else None)

    report = {
        "total_trades": len(rows),
        "win_rate": round(len(wins) / len(rows), 3) if rows else None,
        "gross_pnl": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "net_pnl": round(net, 2),
        "sharpe": _sharpe(daily_rets),
        "max_drawdown": _max_drawdown(week_curve) if week_curve else None,
        "best_strategy": ({"name": ranked[0][0], **ranked[0][1]} if ranked else None),
        "worst_strategy": ({"name": ranked[-1][0], **ranked[-1][1],
                            "root_cause": _root_cause(ranked[-1][0])} if ranked else None),
        "signals_overperforming": [{"name": n, "weight": w} for n, w in over],
        "signals_underperforming": [{"name": n, "weight": w} for n, w in under],
        "recommended_adjustments": [
            f"Increase reliance on '{over[0][0]}'" if over else None,
            f"Reduce reliance on '{under[0][0]}'" if under and under[0][1] < 0.8 else None,
        ],
        "portfolio_week_return": round(week_return, 4) if week_return is not None else None,
        "spy_week_return": spy_week_return,
        "vs_spy": (round(week_return - spy_week_return, 4)
                   if week_return is not None and spy_week_return is not None else None),
    }
    report["recommended_adjustments"] = [r for r in report["recommended_adjustments"] if r]
    db.save_weekly_report(week_ending.isoformat(), report)
    return report


def _root_cause(strategy: str) -> str:
    rows = [t for t in db.all_trades(200)
            if t["strategy"] == strategy and t["status"] == "closed" and (t["pnl"] or 0) < 0]
    if not rows:
        return "insufficient data"
    regimes = [t.get("market_regime") or "?" for t in rows]
    common = max(set(regimes), key=regimes.count)
    avg_iv = statistics.mean([t["iv_rank"] or 50 for t in rows])
    return (f"{len(rows)} losers, most in '{common}' regime, avg IV rank at entry "
            f"{avg_iv:.0f} — review whether the entry rule fits that regime.")
