"""
ml_engine.py — XGBoost-based setup scoring (upgrade spec §2).

Replaces ONLY the magnitude/confidence component of scoring: direction still
comes from the rule engine's composite_score(). Never touches execution, risk,
or options math. Every failure path returns None so signals.score_setup()
falls back to the deterministic rule-based scorer (spec §11).

xgboost is imported lazily so the whole backend keeps running when it isn't
installed. Tests may inject a stand-in via sys.modules["xgboost"].
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import threading
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
_CFG_CACHE: dict | None = None
_MODEL_CACHE: dict = {"model": None, "mtime": None, "meta": None}
_RETRAIN_KICKED = False
_LOCK = threading.Lock()

# Fixed feature order — training and inference must agree (spec §2.2.1, ~22 features).
FEATURE_NAMES = [
    # technical
    "rsi_14", "macd_hist", "bb_position", "adx_14", "vwap_deviation",
    "atr_pct", "price_vs_sma20", "price_vs_sma200",
    # options
    "iv_rank", "iv_percentile", "put_call_ratio", "unusual_flow", "expected_move_pct",
    # macro / regime
    "vix_level", "fear_greed", "regime", "yield_curve",
    # signal consensus
    "bull_signal_count", "bear_signal_count", "signal_consensus",
    "top_signal_strength", "category_coverage",
]

# Spec encoding: 0=trending_bull, 1=trending_bear, 2=ranging, 3=high_vol.
# Our regime classifier doesn't split trending by direction, so 'trending'
# maps via overnight bias when available; crisis folds into high_vol.
_REGIME_CODE = {"trending": 0, "trending_bull": 0, "trending_bear": 1,
                "range_bound": 2, "ranging": 2, "high_volatility": 3,
                "high_vol": 3, "crisis": 3}


def ml_config() -> dict:
    global _CFG_CACHE
    if _CFG_CACHE is None:
        try:
            _CFG_CACHE = json.loads((ROOT / "config.json").read_text()).get("ml_engine", {})
        except Exception:
            _CFG_CACHE = {}
    return _CFG_CACHE


def model_dir() -> Path:
    d = ml_config().get("model_dir", "backend/models")
    p = Path(d)
    return p if p.is_absolute() else ROOT / d


def model_path() -> Path:
    return model_dir() / "signal_model.json"


def meta_path() -> Path:
    return model_dir() / "signal_model_meta.json"


# ----------------------------------------------------------------------------- features

def build_market_state(snap, ctx) -> dict:
    """Flatten SymbolSnapshot + MarketContext into the market_state dict that
    extract_features() consumes. Pure python, no new API calls (spec §2.2.6)."""
    ind = snap.ind
    price = float(snap.price) or 1.0

    def f(v, default=0.0):
        try:
            v = float(v)
            return default if math.isnan(v) else v
        except (TypeError, ValueError):
            return default

    sma20, sma200 = f(ind.get("sma20"), price), f(ind.get("sma200"), price)
    atr14 = f(ind.get("atr14"))
    bb_u, bb_l = f(ind.get("bb_upper"), price), f(ind.get("bb_lower"), price)
    bb_span = (bb_u - bb_l) or 1e-9
    # Current IV proxy = latest realized-vol entry (true chain IV once Alpaca
    # options data is wired in); iv_percentile from the same history.
    iv_now = f(snap.iv_history[-1], 0.2) if snap.iv_history else 0.2
    below = sum(1 for v in snap.iv_history if v <= iv_now)
    iv_pctile = 100.0 * below / len(snap.iv_history) if snap.iv_history else 50.0
    regime = getattr(ctx, "regime_detail", None) or ctx.regime
    if regime == "trending" and ctx.overnight_bias in ("bullish", "bearish"):
        regime = "trending_bull" if ctx.overnight_bias == "bullish" else "trending_bear"

    return {
        "rsi_14": f(ind.get("rsi14"), 50.0),
        "macd_hist": (f(ind.get("macd")) - f(ind.get("macd_sig"))) / (atr14 or 1e-9),
        "bb_position": min(max((price - bb_l) / bb_span, 0.0), 1.0),
        "adx_14": f(ind.get("adx14"), 20.0),
        # VWAP proxy: deviation from 20d SMA until intraday VWAP is stored.
        "vwap_deviation": (price - sma20) / (sma20 or 1e-9) * 100.0,
        "atr_pct": atr14 / price * 100.0,
        "price_vs_sma20": (price - sma20) / (sma20 or 1e-9) * 100.0,
        "price_vs_sma200": (price - sma200) / (sma200 or 1e-9) * 100.0,
        "iv_rank": f(snap.iv_rank, 50.0),
        "iv_percentile": iv_pctile,
        "put_call_ratio": f(ctx.put_call_ratio, 0.9),
        "unusual_flow": 1.0 if snap.unusual_flow_premium >= 500_000 else 0.0,
        "expected_move_pct": iv_now * math.sqrt(30.0 / 365.0) * 100.0,
        "vix_level": f(ctx.vix, 18.0),
        "fear_greed": f(ctx.fear_greed_index, 50.0),
        "regime": float(_REGIME_CODE.get(regime, 2)),
        "yield_curve": f(ctx.yield_curve_2s10s, 0.3),
    }


def extract_features(signals: list, market_state: dict) -> dict:
    """Build the full model feature dict from fired Signal objects plus the
    market_state from build_market_state() (spec §2.2.6)."""
    bull = sum(1 for s in signals if s.direction == "bullish")
    bear = sum(1 for s in signals if s.direction == "bearish")
    feats = {k: float(market_state.get(k, 0.0)) for k in FEATURE_NAMES
             if k not in ("bull_signal_count", "bear_signal_count", "signal_consensus",
                          "top_signal_strength", "category_coverage")}
    feats.update({
        "bull_signal_count": float(bull),
        "bear_signal_count": float(bear),
        "signal_consensus": bull / (bull + bear) if (bull + bear) else 0.5,
        "top_signal_strength": max((s.strength for s in signals), default=0.0),
        "category_coverage": float(len({s.category for s in signals})),
    })
    return {k: feats.get(k, 0.0) for k in FEATURE_NAMES}  # fixed order, no gaps


# ----------------------------------------------------------------------------- model io

def _load_model():
    """Load + cache the deployed model; invalidate cache when the file changes."""
    import xgboost as xgb  # lazy — ImportError handled by callers
    mp = model_path()
    if not mp.exists() or not meta_path().exists():
        return None, None
    mtime = mp.stat().st_mtime
    with _LOCK:
        if _MODEL_CACHE["model"] is not None and _MODEL_CACHE["mtime"] == mtime:
            return _MODEL_CACHE["model"], _MODEL_CACHE["meta"]
        meta = json.loads(meta_path().read_text())
        model = xgb.XGBClassifier()
        model.load_model(str(mp))
        _MODEL_CACHE.update(model=model, mtime=mtime, meta=meta)
        return model, meta


def _kick_async_retrain():
    """Stale model → retrain in the background, at most once per process."""
    global _RETRAIN_KICKED
    if _RETRAIN_KICKED:
        return
    _RETRAIN_KICKED = True

    def _run():
        try:
            from retrain_worker import maybe_retrain
            maybe_retrain(force=True)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True, name="ml-retrain").start()


def status() -> dict:
    """For the dashboard Model Performance panel (spec §7)."""
    out = {"enabled": bool(ml_config().get("enabled", True)), "available": False,
           "stale": None, "trained_at": None, "n_samples": None, "val_auc": None,
           "scoring_method_current": "rules"}
    try:
        model, meta = _load_model()
    except ImportError:
        out["note"] = "xgboost not installed"
        return out
    if model is None or meta is None:
        return out
    out.update(available=True, trained_at=meta.get("trained_at"),
               n_samples=meta.get("n_samples"), val_auc=meta.get("val_auc"))
    try:
        age = (dt.datetime.now(dt.timezone.utc)
               - dt.datetime.fromisoformat(meta["trained_at"])).days
        out["stale"] = age > int(ml_config().get("feature_staleness_days", 14))
    except Exception:
        out["stale"] = True
    if out["enabled"] and not out["stale"]:
        out["scoring_method_current"] = "ml"
    return out


# ----------------------------------------------------------------------------- scoring

def score(features: dict) -> Optional[float]:
    """Probability of profitable outcome (0.0–1.0) from the deployed model, or
    None whenever the rules fallback should be used instead (spec §2.2.5):
    disabled in config, xgboost missing, no/corrupt model, stale model (>14d,
    triggers async retrain), or any extraction/prediction error."""
    if not ml_config().get("enabled", True):
        return None
    try:
        model, meta = _load_model()
        if model is None or meta is None:
            return None
        trained = dt.datetime.fromisoformat(meta["trained_at"])
        age_days = (dt.datetime.now(dt.timezone.utc) - trained).days
        if age_days > int(ml_config().get("feature_staleness_days", 14)):
            _kick_async_retrain()
            return None
        names = meta.get("feature_names", FEATURE_NAMES)
        row = [[float(features.get(k, 0.0)) for k in names]]
        proba = model.predict_proba(row)[0][1]
        return float(min(max(proba, 0.0), 1.0))
    except ImportError:
        return None
    except Exception:
        try:
            import db
            db.log_event("warn", "ml", "score() failed; using rules fallback")
        except Exception:
            pass
        return None
