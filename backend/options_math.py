"""
options_math.py — Black-Scholes pricing, Greeks, IV solve, IV rank, expected move,
delta-targeted strike selection, OCC symbol construction. Stdlib-only (math.erf),
so it runs anywhere QuantLib doesn't.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Iterable, Optional


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_price(S: float, K: float, t: float, r: float, iv: float, kind: str) -> float:
    """t in years; kind 'call' | 'put'."""
    if t <= 0 or iv <= 0:
        intrinsic = max(S - K, 0.0) if kind == "call" else max(K - S, 0.0)
        return intrinsic
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    if kind == "call":
        return S * _norm_cdf(d1) - K * math.exp(-r * t) * _norm_cdf(d2)
    return K * math.exp(-r * t) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_greeks(S: float, K: float, t: float, r: float, iv: float, kind: str) -> dict:
    if t <= 0 or iv <= 0:
        delta = (1.0 if S > K else 0.0) if kind == "call" else (-1.0 if S < K else 0.0)
        return {"delta": delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    sqrt_t = math.sqrt(t)
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * t) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    pdf = _norm_pdf(d1)
    gamma = pdf / (S * iv * sqrt_t)
    vega = S * pdf * sqrt_t / 100.0  # per 1 vol-point
    if kind == "call":
        delta = _norm_cdf(d1)
        theta = (-S * pdf * iv / (2 * sqrt_t) - r * K * math.exp(-r * t) * _norm_cdf(d2)) / 365.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (-S * pdf * iv / (2 * sqrt_t) + r * K * math.exp(-r * t) * _norm_cdf(-d2)) / 365.0
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


def implied_vol(price: float, S: float, K: float, t: float, r: float, kind: str,
                lo: float = 0.005, hi: float = 5.0, tol: float = 1e-5) -> Optional[float]:
    """Bisection IV solve. Returns None if the price is outside no-arbitrage bounds."""
    if t <= 0:
        return None
    if bs_price(S, K, t, r, lo, kind) > price or bs_price(S, K, t, r, hi, kind) < price:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if bs_price(S, K, t, r, mid, kind) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


def iv_rank(current_iv: float, iv_history_252d: Iterable[float]) -> float:
    """(current - 52w low) / (52w high - 52w low), 0-100."""
    hist = list(iv_history_252d)
    if not hist:
        return 50.0
    lo, hi = min(hist), max(hist)
    if hi == lo:
        return 50.0
    return round(100.0 * (current_iv - lo) / (hi - lo), 1)


def iv_percentile(current_iv: float, iv_history_252d: Iterable[float]) -> float:
    hist = list(iv_history_252d)
    if not hist:
        return 50.0
    below = sum(1 for v in hist if v < current_iv)
    return round(100.0 * below / len(hist), 1)


def expected_move(price: float, iv: float, dte: int) -> float:
    """1-SD expected move in dollars."""
    return price * iv * math.sqrt(max(dte, 0.5) / 365.0)


def strike_for_delta(S: float, t: float, r: float, iv: float, kind: str,
                     target_delta: float, strike_increment: float = 1.0) -> float:
    """Find the listed strike whose BS delta is closest to target (abs value for puts)."""
    target = abs(target_delta)
    lo, hi = S * 0.5, S * 1.5
    best_k, best_err = S, 1e9
    k = math.floor(lo / strike_increment) * strike_increment
    while k <= hi:
        if k > 0:
            d = abs(bs_greeks(S, k, t, r, iv, kind)["delta"])
            err = abs(d - target)
            if err < best_err:
                best_err, best_k = err, k
        k += strike_increment
    return round(best_k, 2)


def occ_symbol(root: str, expiry: dt.date, kind: str, strike: float) -> str:
    """OCC option symbol, e.g. AAPL260620C00220000 (Alpaca format, no padding spaces)."""
    return (f"{root.upper()}{expiry.strftime('%y%m%d')}"
            f"{'C' if kind == 'call' else 'P'}{int(round(strike * 1000)):08d}")


def parse_occ_symbol(symbol: str) -> dict:
    """Inverse of occ_symbol for Alpaca-style symbols."""
    i = next(idx for idx, ch in enumerate(symbol) if ch.isdigit())
    root = symbol[:i]
    expiry = dt.datetime.strptime(symbol[i:i + 6], "%y%m%d").date()
    kind = "call" if symbol[i + 6] == "C" else "put"
    strike = int(symbol[i + 7:]) / 1000.0
    return {"root": root, "expiry": expiry, "kind": kind, "strike": strike}


def strike_increment_for(price: float) -> float:
    if price < 25:
        return 0.5
    if price < 200:
        return 1.0
    return 5.0
