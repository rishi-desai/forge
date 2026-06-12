"""
signals.py — Multi-factor signal engine (spec Layer 2 + §2.2 rules).

Technical indicators are implemented directly in pandas (no TA-Lib build step
required; if pandas_ta is installed the results are equivalent). Each rule from
the spec emits a Signal; the composite engine combines them using weights the
learning loop adjusts over time.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

BULLISH, BEARISH, NEUTRAL = "bullish", "bearish", "neutral"


# ----------------------------------------------------------------------------- indicators

def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / length, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / length, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-12)
    return 100 - 100 / (1 + rs)


def macd(close: pd.Series, fast=12, slow=26, signal=9) -> pd.DataFrame:
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()
    line = ema_f - ema_s
    sig = line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def bbands(close: pd.Series, length=20, std=2.0) -> pd.DataFrame:
    mid = close.rolling(length).mean()
    sd = close.rolling(length).std()
    return pd.DataFrame({"upper": mid + std * sd, "mid": mid, "lower": mid - std * sd})


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length=14) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def adx(high: pd.Series, low: pd.Series, close: pd.Series, length=14) -> pd.Series:
    up, down = high.diff(), -low.diff()
    plus_dm = ((up > down) & (up > 0)) * up
    minus_dm = ((down > up) & (down > 0)) * down
    tr = atr(high, low, close, length)
    plus_di = 100 * plus_dm.ewm(alpha=1 / length, adjust=False).mean() / tr
    minus_di = 100 * minus_dm.ewm(alpha=1 / length, adjust=False).mean() / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-12)
    return dx.ewm(alpha=1 / length, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """Intraday df with high/low/close/volume, session-anchored."""
    tp = (df.high + df.low + df.close) / 3
    return (tp * df.volume).cumsum() / df.volume.cumsum()


def compute_indicator_frame(daily: pd.DataFrame) -> pd.DataFrame:
    """daily: OHLCV DataFrame (ascending dates). Returns enriched copy."""
    df = daily.copy()
    df["rsi14"] = rsi(df.close)
    m = macd(df.close)
    df["macd"], df["macd_sig"] = m["macd"], m["signal"]
    bb = bbands(df.close)
    df["bb_upper"], df["bb_lower"] = bb["upper"], bb["lower"]
    df["atr14"] = atr(df.high, df.low, df.close)
    df["adx14"] = adx(df.high, df.low, df.close)
    for n in (20, 50, 200):
        df[f"sma{n}"] = df.close.rolling(n).mean()
    df["vol_avg50"] = df.volume.rolling(50).mean()
    df["high_52w"] = df.high.rolling(252, min_periods=60).max()
    df["low_52w"] = df.low.rolling(252, min_periods=60).min()
    return df


# ----------------------------------------------------------------------------- signal objects

@dataclass
class Signal:
    name: str
    category: str          # technical | options | macro | sentiment | foreign
    direction: str         # bullish | bearish | neutral
    strength: float        # 0-1 raw confidence of this single rule
    strategy_hint: Optional[str] = None
    detail: str = ""


@dataclass
class MarketContext:
    vix: float = 18.0
    vix_term: dict = field(default_factory=dict)   # {"VIX9D":..,"VIX":..,"VIX3M":..}
    fear_greed_index: float = 50.0
    put_call_ratio: float = 0.9
    yield_curve_2s10s: float = 0.3
    dxy_trend: str = NEUTRAL
    usdjpy_trend: str = NEUTRAL
    eurusd_trend: str = NEUTRAL
    regime: str = "trending"
    regime_detail: str | None = None      # trending_bull/bear, ranging, high_vol (model)
    regime_confidence: float | None = None
    regime_source: str = "rules"          # rules | model | override
    overnight_bias: str = NEUTRAL
    overnight_summary: str = ""


def classify_regime(vix: float, adx_value: float) -> str:
    if vix > 35:
        return "crisis"
    if vix > 25:
        return "high_volatility"
    if adx_value >= 25:
        return "trending"
    return "range_bound"


# ----------------------------------------------------------------------------- rule engine (spec §2.2)

@dataclass
class SymbolSnapshot:
    """Everything the rules need about one underlying right now."""
    symbol: str
    price: float
    ind: pd.Series                 # last row of compute_indicator_frame
    iv_rank: float = 50.0
    iv_history: list = field(default_factory=list)
    earnings_in_days: Optional[int] = None
    rs_rank: float = 50.0          # relative strength vs SPY percentile
    unusual_flow_premium: float = 0.0
    unusual_flow_direction: str = NEUTRAL
    iv_crush_history: bool = False
    sector_outperform_days: int = 0
    news_sentiment: float = 0.0    # -1..1 from FinBERT
    intraday_above_vwap: Optional[bool] = None
    open_gap_pct: Optional[float] = None


def technical_and_options_rules(s: SymbolSnapshot, ctx: MarketContext,
                                now_et: Optional[dt.datetime] = None) -> list[Signal]:
    """Explicit if/then mappings from spec §2.2. Each fired rule → one Signal."""
    out: list[Signal] = []
    i = s.ind

    def ok(*cols):
        return all(pd.notna(i.get(c)) for c in cols)

    # --- Bullish options entries
    if ok("rsi14", "sma200") and i.rsi14 < 35 and s.price > i.sma200 and s.iv_rank < 30:
        out.append(Signal("oversold_in_uptrend", "technical", BULLISH, 0.75,
                          "long_call", f"RSI {i.rsi14:.0f} < 35, price above 200d MA, IV rank {s.iv_rank:.0f}"))
    if (s.earnings_in_days is not None and 5 <= s.earnings_in_days <= 14
            and s.iv_rank < 40 and ok("sma50") and s.price > i.sma50):
        out.append(Signal("pre_earnings_iv_expansion", "options", BULLISH, 0.65,
                          "bull_call_spread",
                          f"Earnings in {s.earnings_in_days}d, IV rank {s.iv_rank:.0f} (cheap), uptrend"))
    if s.unusual_flow_premium > 500_000 and s.unusual_flow_direction == BULLISH:
        out.append(Signal("institutional_call_flow", "options", BULLISH, 0.85,
                          "bull_call_spread",
                          f"${s.unusual_flow_premium/1e6:.1f}M unusual call premium"))

    # --- Bearish options entries
    if ok("rsi14", "sma20") and i.rsi14 > 70 and s.price < i.sma20 and s.iv_rank < 30:
        out.append(Signal("overbought_failed_reclaim", "technical", BEARISH, 0.75,
                          "long_put", f"RSI {i.rsi14:.0f} > 70 under 20d MA, IV rank {s.iv_rank:.0f}"))
    if (s.symbol == "SPY" and ok("sma50", "vol_avg50") and s.price < i.sma50
            and i.volume > i.vol_avg50 and ctx.vix < 20):
        out.append(Signal("spy_50d_breakdown_cheap_vol", "technical", BEARISH, 0.80,
                          "bear_put_spread", "SPY below 50d MA on volume; VIX < 20 → puts cheap"))
    if s.unusual_flow_premium > 500_000 and s.unusual_flow_direction == BEARISH:
        out.append(Signal("institutional_put_flow", "options", BEARISH, 0.85,
                          "bear_put_spread",
                          f"${s.unusual_flow_premium/1e6:.1f}M unusual put premium"))

    # --- Premium selling (IV-rank driven)
    no_earnings_14d = s.earnings_in_days is None or s.earnings_in_days > 14
    sideways = ok("adx14") and i.adx14 < 20
    if s.iv_rank > 60 and no_earnings_14d and sideways:
        out.append(Signal("high_iv_rangebound", "options", NEUTRAL, 0.80, "iron_condor",
                          f"IV rank {s.iv_rank:.0f} > 60, ADX {i.adx14:.0f} (sideways), no earnings 14d"))
    if s.iv_rank > 50 and ok("sma50") and s.price > i.sma50:
        out.append(Signal("rich_iv_bullish_underlying", "options", BULLISH, 0.70,
                          "cash_secured_put",
                          f"IV rank {s.iv_rank:.0f} > 50 with price above 50d MA"))
    if s.iv_rank > 70 and s.iv_crush_history and s.earnings_in_days is not None \
            and s.earnings_in_days <= 10:
        # SPEC DEVIATION: spec says short strangle (naked both sides) — impossible
        # in a cash account by the spec's own rules. Defined-risk equivalent:
        out.append(Signal("pre_earnings_iv_crush", "options", NEUTRAL, 0.70, "iron_condor",
                          f"IV rank {s.iv_rank:.0f} > 70 with IV-crush history; defined-risk "
                          "condor instead of naked strangle (cash account)"))

    # --- Equity entries
    if (ok("high_52w", "vol_avg50") and s.price >= 0.999 * i.high_52w
            and i.volume > 1.5 * i.vol_avg50 and s.rs_rank > 85):
        out.append(Signal("canslim_breakout", "technical", BULLISH, 0.80, "equity_momentum",
                          f"52w high on {i.volume/i.vol_avg50:.1f}x volume, RS rank {s.rs_rank:.0f}"))
    if ok("rsi14", "low_52w") and i.rsi14 < 30 and s.price <= 1.05 * i.low_52w:
        out.append(Signal("mean_reversion_washout", "technical", BULLISH, 0.55,
                          "equity_mean_reversion", f"RSI {i.rsi14:.0f} near 52w low"))
    if s.sector_outperform_days >= 5:
        out.append(Signal("sector_rotation", "technical", BULLISH, 0.60, "equity_momentum",
                          f"Sector outperforming SPY {s.sector_outperform_days} straight days"))

    # --- 0DTE intraday (SPY/QQQ — SPX not tradable on Alpaca)
    if s.symbol in ("SPY", "QQQ") and now_et is not None:
        t = now_et.time()
        if (s.open_gap_pct is not None and abs(s.open_gap_pct) < 0.3 and ctx.vix < 18
                and dt.time(9, 45) <= t <= dt.time(10, 15)):
            out.append(Signal("0dte_flat_open_condor", "options", NEUTRAL, 0.65,
                              "0dte_iron_condor",
                              f"Flat open ({s.open_gap_pct:+.2f}%), VIX {ctx.vix:.1f} < 18"))
        if s.intraday_above_vwap is not None and t >= dt.time(10, 30):
            d = BULLISH if s.intraday_above_vwap else BEARISH
            out.append(Signal("0dte_vwap_trend", "technical", d, 0.60, "0dte_debit_spread",
                              f"Holding {'above' if s.intraday_above_vwap else 'below'} VWAP after 10:30"))

    # --- Sentiment overlay
    if abs(s.news_sentiment) >= 0.5:
        d = BULLISH if s.news_sentiment > 0 else BEARISH
        out.append(Signal("news_sentiment", "sentiment", d, min(abs(s.news_sentiment), 1.0) * 0.6,
                          None, f"FinBERT news score {s.news_sentiment:+.2f}"))
    return out


def macro_signals(ctx: MarketContext) -> list[Signal]:
    out = []
    if ctx.yield_curve_2s10s < 0:
        out.append(Signal("yield_curve_inverted", "macro", BEARISH, 0.5, None,
                          f"2s10s at {ctx.yield_curve_2s10s:+.2f}"))
    if ctx.vix_term and ctx.vix_term.get("VIX9D", 0) > ctx.vix_term.get("VIX", 99):
        out.append(Signal("vix_backwardation", "macro", BEARISH, 0.7, None,
                          "VIX term structure inverted (panic regime)"))
    if ctx.put_call_ratio > 1.2:
        out.append(Signal("extreme_put_call", "macro", BULLISH, 0.5, None,
                          f"Put/call {ctx.put_call_ratio:.2f} — contrarian bullish"))
    if ctx.fear_greed_index < 20:
        out.append(Signal("extreme_fear", "macro", BULLISH, 0.45, None,
                          f"Fear & Greed {ctx.fear_greed_index:.0f} — contrarian"))
    elif ctx.fear_greed_index > 80:
        out.append(Signal("extreme_greed", "macro", BEARISH, 0.45, None,
                          f"Fear & Greed {ctx.fear_greed_index:.0f} — contrarian"))
    if ctx.dxy_trend == BULLISH:
        out.append(Signal("dxy_strength", "macro", BEARISH, 0.35, None,
                          "DXY rising — headwind for commodities/EM"))
    return out


def foreign_overnight_signals(etf_moves: dict, fx: dict) -> tuple[list[Signal], str, str]:
    """etf_moves: {"EWJ": -1.7, ...} overnight %; fx: {"USDJPY": -0.6, "EURUSD": -0.3, "DXY": 0.4}.
    Returns (signals, bias, one-sentence summary) per spec §2.4."""
    out, score = [], 0.0
    rules = {
        "EWJ": (-1.5, BEARISH, "Nikkei proxy down — defensive bias at US open"),
        "EWG": (1.0, BULLISH, "DAX proxy up — bullish pre-market lean for US tech"),
    }
    for sym, (thresh, direction, msg) in rules.items():
        mv = etf_moves.get(sym)
        if mv is None:
            continue
        if (thresh < 0 and mv <= thresh) or (thresh > 0 and mv >= thresh):
            out.append(Signal(f"overnight_{sym.lower()}", "foreign", direction, 0.5, None,
                              f"{sym} {mv:+.1f}%: {msg}"))
            score += 1 if direction == BULLISH else -1
    if fx.get("USDJPY", 0) < -0.4:
        out.append(Signal("usdjpy_riskoff", "foreign", BEARISH, 0.5, None,
                          "USD/JPY falling — risk-off; raise put-hedge probability"))
        score -= 1
    if fx.get("EURUSD", 0) < -0.5:
        out.append(Signal("eurusd_riskoff", "foreign", BEARISH, 0.4, None,
                          "EUR/USD falling sharply — European risk-off bleeding into US open"))
        score -= 0.5
    avg = sum(etf_moves.values()) / len(etf_moves) if etf_moves else 0.0
    score += max(min(avg, 1.5), -1.5) * 0.5
    bias = BULLISH if score > 0.5 else BEARISH if score < -0.5 else NEUTRAL
    summary = (f"Overnight bias {bias.upper()}: foreign ETF proxies averaged {avg:+.1f}%, "
               f"{len(out)} overlay rule(s) fired.")
    return out, bias, summary


# ----------------------------------------------------------------------------- composite

DEFAULT_WEIGHTS = {"technical": 1.0, "options": 1.0, "macro": 0.6,
                   "sentiment": 0.5, "foreign": 0.4}


def composite_score(signals: list[Signal], weights: dict | None = None,
                    signal_weights: dict | None = None) -> dict:
    """Combine fired signals into directional composite strength 0-1.

    weights: category weights; signal_weights: per-rule learned weights from DB.
    Returns {direction, strength, bull, bear, signals}.
    """
    weights = weights or DEFAULT_WEIGHTS
    signal_weights = signal_weights or {}
    bull = bear = 0.0
    for s in signals:
        w = weights.get(s.category, 0.5) * signal_weights.get(s.name, 1.0) * s.strength
        if s.direction == BULLISH:
            bull += w
        elif s.direction == BEARISH:
            bear += w
        else:
            bull += w * 0.5
            bear += w * 0.5
    total = bull + bear
    if total == 0:
        return {"direction": NEUTRAL, "strength": 0.0, "bull": 0, "bear": 0, "signals": signals}
    direction = BULLISH if bull > bear else BEARISH if bear > bull else NEUTRAL
    dominance = abs(bull - bear) / total          # how one-sided
    magnitude = 1 - math.exp(-0.55 * total)       # saturating count/“how much fired”
    strength = round(min(0.30 + 0.70 * (0.5 * dominance + 0.5 * magnitude), 1.0), 3)
    return {"direction": direction, "strength": strength,
            "bull": round(bull, 3), "bear": round(bear, 3), "signals": signals}


def score_setup(signals: list[Signal], market_state: dict,
                weights: dict | None = None,
                signal_weights: dict | None = None) -> dict:
    """Primary scoring entry point (ML upgrade spec §3.1). Routes magnitude
    scoring through the trained XGBoost model when one is deployed and fresh;
    falls back to the deterministic composite_score() otherwise.

    Same return shape as composite_score() plus:
      scoring_method: "ml" | "rules"   (logged on each trade for comparison)
      ml_score:       raw model probability when method == "ml"
      features:       the feature dict used (persisted as feature_snapshot so
                      the training set can be rebuilt from trade history — this
                      is recorded on the rules path too, which is what lets the
                      first 30+ rules-scored trades bootstrap the model)

    Direction always comes from the rule engine; the model only replaces the
    confidence/magnitude component. The ML layer has no authority over what
    gets traded, how much, or when.
    """
    features = None
    try:
        from ml_engine import extract_features, score
        features = extract_features(signals, market_state)
        ml_strength = score(features)
        if ml_strength is not None:
            base = composite_score(signals, weights, signal_weights)
            base.update(strength=round(float(ml_strength), 3),
                        scoring_method="ml", ml_score=round(float(ml_strength), 4),
                        features=features)
            return base
    except ImportError:
        pass          # ml_engine/xgboost absent → rules
    except Exception:
        features = features or None   # any ML failure → rules (spec §11)

    result = composite_score(signals, weights, signal_weights)
    result.update(scoring_method="rules", ml_score=None, features=features)
    return result
