"""
test_regime.py — Regime model verification on synthetic data with planted
regimes: a calm uptrend, a high-vol crash, and low-vol chop. Verifies the
unsupervised cluster→label mapping recovers them, plus persistence, the VIX
circuit-breaker override, every fallback path, and vocabulary compatibility.

Run: python tests/test_regime.py
"""

import datetime as dt
import json
import os
import sys
import tempfile

import numpy as np
import pandas as pd

BASE = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(BASE, "backend"))

TMP = tempfile.mkdtemp(prefix="regimetest_")
os.environ["DB_PATH"] = os.path.join(TMP, "trading.db")

import regime_model as rm  # noqa: E402
from regime_model import LEGACY_MAP, RegimeModel, build_regime_features  # noqa: E402

# Isolate config + model dir
_TEST_CFG = {"enabled": True, "n_components": 4, "lookback_days": 700,
             "refit_every_days": 7, "min_history_days": 120, "model_dir": TMP}
rm._cfg = lambda: _TEST_CFG

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ok   {name}")
    else:    FAIL += 1; print(f"  FAIL {name}  {detail}")


def synth_market(seed=3) -> pd.DataFrame:
    """760 trading days: calm uptrend → chop → crash → calm uptrend."""
    rng = np.random.default_rng(seed)
    segs = [
        (260, 0.0009, 0.007),    # calm bull: +0.09%/d drift, 0.7% daily vol
        (180, 0.0000, 0.006),    # low-vol chop
        (120, -0.0045, 0.030),   # crash: -0.45%/d, 3% daily vol
        (200, 0.0010, 0.008),    # recovery bull
    ]
    rets = np.concatenate([rng.normal(m, s, n) for n, m, s in segs])
    close = 400 * np.exp(np.cumsum(rets))
    intraday = np.abs(rng.normal(0, 0.004, len(close)))
    return pd.DataFrame({
        "open": close * (1 - intraday / 2),
        "high": close * (1 + intraday),
        "low": close * (1 - intraday),
        "close": close,
        "volume": rng.integers(60e6, 120e6, len(close)).astype(float),
    })


df = synth_market()

print("== Feature builder ==")
feats = build_regime_features(df)
check("five features, no NaNs", list(feats.columns) == rm.REGIME_FEATURES
      and not feats.isna().any().any())
check("rows align with usable history", 400 < len(feats) <= len(df))

print("== Fit + cluster→label mapping ==")
model = RegimeModel()
check("fresh model unavailable before fit", not model.status()["available"])
ok = model.fit(df)
check("fit succeeds on synthetic history", ok)
st = model.status()
labels_assigned = set((st["label_map"] or {}).values())
check("all four labels assigned", labels_assigned == set(rm.LABELS), labels_assigned)
cs = st["cluster_stats"]
hv_cluster = [k for k, v in st["label_map"].items() if v == "high_vol"][0]
check("high_vol cluster has the max realized vol",
      cs[hv_cluster]["rv20"] == max(c["rv20"] for c in cs.values()))
bull = [k for k, v in st["label_map"].items() if v == "trending_bull"][0]
check("trending_bull cluster has positive mean 20d return",
      cs[bull]["ret20"] > 0, cs[bull]["ret20"])

print("== Classification of planted segments ==")
# End of crash segment: days 0..559 (260+180+120) → classify at day ~555
crash_view = df.iloc[:556]
r = model.classify_frame(crash_view, vix=28.0)
check("crash segment → high_vol detail", r["detail"] == "high_vol", r)
check("crash maps to legacy 'high_volatility'", r["regime"] == "high_volatility")
check("source is model with confidence", r["source"] == "model"
      and 0 < r["confidence"] <= 1)
# End of full series = recovery bull
r2 = model.classify_frame(df, vix=14.0)
check("recovery segment → bullish/calm regime",
      r2["detail"] in ("trending_bull", "ranging"), r2)
# Chop segment end (day ~438 = 260+178)
r3 = model.classify_frame(df.iloc[:438], vix=14.0)
check("chop segment → non-crash regime",
      r3["detail"] in ("ranging", "trending_bull", "trending_bear"), r3)
check("legacy vocabulary respected on all outputs",
      all(x["regime"] in ("crisis", "high_volatility", "trending", "range_bound")
          for x in (r, r2, r3)))

print("== Deterministic VIX override outranks the model ==")
r4 = model.classify_frame(df, vix=41.0)
check("VIX>35 → crisis regardless of cluster", r4["regime"] == "crisis"
      and r4["source"] == "override" and r4["confidence"] == 1.0)

print("== Persistence round-trip ==")
expect = model.classify_frame(df, vix=14.0)
m2 = RegimeModel()           # fresh instance loads from disk
check("reloaded model available", m2.status()["available"])
got = m2.classify_frame(df, vix=14.0)
check("identical classification after reload",
      got["detail"] == expect["detail"]
      and abs(got["confidence"] - expect["confidence"]) < 1e-9)

print("== Refit cadence ==")
check("fresh model does not refit early", not m2.maybe_refit(df))
m2._meta["trained_at"] = (dt.datetime.now(dt.timezone.utc)
                          - dt.timedelta(days=10)).isoformat()
check("stale (>7d) model refits", m2.maybe_refit(df))

print("== Fallbacks ==")
_TEST_CFG["enabled"] = False
r5 = m2.classify_frame(df, vix=22.0, adx_value=30.0)
check("disabled → rules fallback", r5["source"] == "rules"
      and r5["regime"] in ("trending", "high_volatility", "range_bound"))
_TEST_CFG["enabled"] = True
m3 = RegimeModel.__new__(RegimeModel)
import threading
m3._lock, m3._gmm, m3._meta = threading.Lock(), None, None
r6 = m3.classify_frame(df, vix=20.0, adx_value=15.0)
check("no model → rules fallback (range_bound at ADX 15)",
      r6["source"] == "rules" and r6["regime"] == "range_bound")
check("thin history refuses to fit", not m3.fit(df.iloc[:100]))
short = m3.classify_frame(df.iloc[:30], vix=18.0, adx_value=30.0)
check("insufficient feature history → rules fallback", short["source"] == "rules")

print("== ML feature encoding consumes the detail label ==")
import ml_engine
check("regime codes cover all model labels",
      {l: ml_engine._REGIME_CODE[l] for l in rm.LABELS}
      == {"trending_bull": 0, "trending_bear": 1, "ranging": 2, "high_vol": 3})

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
