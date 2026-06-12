"""
regime_model.py — Layer 3 of the target stack: a learned market-regime
classifier replacing (when trained) the deterministic VIX/ADX rule.

Approach: unsupervised GaussianMixture (sklearn) over five daily market-level
features computed from SPY history. Clusters are mapped to the four regime
labels the ML feature spec expects (trending_bull / trending_bear / ranging /
high_vol) by their statistics — the highest-volatility cluster is high_vol,
then highest/lowest mean 20-day return are trending_bull/bear, the leftover is
ranging. A GMM is "HMM without the transition matrix": no temporal smoothing,
but sklearn-native, tiny, and trainable on 2 years of free daily data. The
upgrade path to a true HMM (hmmlearn) is a drop-in swap of `_fit`.

System philosophy preserved:
  * Deterministic override: VIX > 35 is ALWAYS 'crisis' regardless of the model
    — circuit breakers stay dumb and absolute.
  * Fallback: any failure (disabled, sklearn missing, thin history, corrupt
    model) → signals.classify_regime(). The bot can never crash here.
  * Additive: risk, execution, strategy selection untouched; consumers keep the
    legacy vocabulary via LEGACY_MAP; the directional detail feeds the ML
    `regime` feature.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from signals import adx as _adx, classify_regime

ROOT = Path(__file__).resolve().parent.parent

REGIME_FEATURES = ["ret20", "rv20", "trend50", "adx14", "drawdown"]
LABELS = ["trending_bull", "trending_bear", "ranging", "high_vol"]
LEGACY_MAP = {"trending_bull": "trending", "trending_bear": "trending",
              "ranging": "range_bound", "high_vol": "high_volatility"}


def _cfg() -> dict:
    try:
        return json.loads((ROOT / "config.json").read_text()).get("regime_model", {})
    except Exception:
        return {}


def _model_dir() -> Path:
    d = Path(_cfg().get("model_dir", "backend/models"))
    return d if d.is_absolute() else ROOT / d


def build_regime_features(daily: pd.DataFrame) -> pd.DataFrame:
    """daily: SPY OHLCV ascending. Returns one row per day of regime features."""
    df = daily.copy()
    rets = df.close.pct_change()
    out = pd.DataFrame(index=df.index)
    out["ret20"] = df.close.pct_change(20)
    out["rv20"] = rets.rolling(20).std() * np.sqrt(252)
    out["trend50"] = df.close / df.close.rolling(50).mean() - 1.0
    out["adx14"] = _adx(df.high, df.low, df.close)
    out["drawdown"] = df.close / df.close.rolling(252, min_periods=60).max() - 1.0
    return out.dropna()


class RegimeModel:
    def __init__(self):
        self._lock = threading.Lock()
        self._gmm = None
        self._meta: Optional[dict] = None
        self._load()

    # ------------------------------------------------------------- persistence

    def _paths(self):
        return _model_dir() / "regime_model.pkl", _model_dir() / "regime_model_meta.json"

    def _load(self):
        pkl, meta = self._paths()
        try:
            if pkl.exists() and meta.exists():
                import joblib
                with self._lock:
                    self._gmm = joblib.load(pkl)
                    self._meta = json.loads(meta.read_text())
        except Exception:
            self._gmm, self._meta = None, None   # corrupt → fallback + refit later

    def _save(self):
        import joblib
        _model_dir().mkdir(parents=True, exist_ok=True)
        pkl, meta = self._paths()
        fd, tmp = tempfile.mkstemp(dir=_model_dir()); os.close(fd)
        joblib.dump(self._gmm, tmp); os.replace(tmp, pkl)
        fd, tmpm = tempfile.mkstemp(dir=_model_dir()); os.close(fd)
        Path(tmpm).write_text(json.dumps(self._meta, indent=1)); os.replace(tmpm, meta)

    # ------------------------------------------------------------- training

    def _stale(self) -> bool:
        if not self._meta:
            return True
        try:
            age = (dt.datetime.now(dt.timezone.utc)
                   - dt.datetime.fromisoformat(self._meta["trained_at"])).days
            return age >= int(_cfg().get("refit_every_days", 7))
        except Exception:
            return True

    def maybe_refit(self, spy_daily: pd.DataFrame) -> bool:
        """Refit weekly (or when no/corrupt model). Returns True if refit ran."""
        if not _cfg().get("enabled", True) or not self._stale():
            return False
        return self.fit(spy_daily)

    def fit(self, spy_daily: pd.DataFrame) -> bool:
        cfg = _cfg()
        feats = build_regime_features(spy_daily)
        feats = feats.tail(int(cfg.get("lookback_days", 504)))
        if len(feats) < int(cfg.get("min_history_days", 120)):
            return False
        try:
            from sklearn.mixture import GaussianMixture
        except ImportError:
            return False

        X = feats[REGIME_FEATURES].to_numpy()
        mu, sd = X.mean(axis=0), X.std(axis=0) + 1e-12
        Xz = (X - mu) / sd
        gmm = GaussianMixture(n_components=int(cfg.get("n_components", 4)),
                              covariance_type="full", n_init=3, random_state=42)
        gmm.fit(Xz)

        # Map clusters → named regimes by their statistics in ORIGINAL units.
        comp = gmm.predict(Xz)
        stats = pd.DataFrame(X, columns=REGIME_FEATURES).groupby(comp).mean()
        label_map: dict[int, str] = {}
        order = list(stats.index)
        hv = stats["rv20"].idxmax()
        label_map[int(hv)] = "high_vol"
        rest = [c for c in order if c != hv]
        if rest:
            bull = stats.loc[rest, "ret20"].idxmax()
            label_map[int(bull)] = "trending_bull"
            rest = [c for c in rest if c != bull]
        if rest:
            bear = stats.loc[rest, "ret20"].idxmin()
            label_map[int(bear)] = "trending_bear"
            rest = [c for c in rest if c != bear]
        for c in rest:
            label_map[int(c)] = "ranging"

        with self._lock:
            self._gmm = gmm
            self._meta = {"trained_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                          "n_samples": len(feats), "feature_mu": mu.tolist(),
                          "feature_sd": sd.tolist(),
                          "label_map": {str(k): v for k, v in label_map.items()},
                          "cluster_stats": {str(k): {f: round(float(v), 5)
                                            for f, v in row.items()}
                                            for k, row in stats.iterrows()}}
            self._save()
        try:
            import db
            db.log_event("info", "regime",
                         f"regime model refit: {len(feats)} days, "
                         f"map={self._meta['label_map']}")
        except Exception:
            pass
        return True

    # ------------------------------------------------------------- inference

    def classify_frame(self, spy_daily: pd.DataFrame, vix: float,
                       adx_value: float = 25.0) -> dict:
        """Classify today's regime. Returns
        {regime (legacy vocab), detail, confidence, source}."""
        if vix > 35:   # deterministic circuit breaker outranks any model
            return {"regime": "crisis", "detail": "high_vol",
                    "confidence": 1.0, "source": "override"}
        if _cfg().get("enabled", True) and self._gmm is not None and self._meta:
            try:
                feats = build_regime_features(spy_daily)
                if len(feats):
                    x = feats[REGIME_FEATURES].iloc[-1].to_numpy()
                    mu = np.array(self._meta["feature_mu"])
                    sd = np.array(self._meta["feature_sd"])
                    post = self._gmm.predict_proba(((x - mu) / sd).reshape(1, -1))[0]
                    k = int(np.argmax(post))
                    detail = self._meta["label_map"].get(str(k), "ranging")
                    return {"regime": LEGACY_MAP[detail], "detail": detail,
                            "confidence": round(float(post[k]), 3), "source": "model"}
            except Exception:
                pass
        legacy = classify_regime(vix, adx_value)
        return {"regime": legacy, "detail": None, "confidence": None, "source": "rules"}

    def status(self) -> dict:
        return {"enabled": bool(_cfg().get("enabled", True)),
                "available": self._gmm is not None,
                "trained_at": (self._meta or {}).get("trained_at"),
                "n_samples": (self._meta or {}).get("n_samples"),
                "label_map": (self._meta or {}).get("label_map"),
                "cluster_stats": (self._meta or {}).get("cluster_stats")}
