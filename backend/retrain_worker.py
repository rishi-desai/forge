"""
retrain_worker.py — Weekly retraining pipeline (upgrade spec §2.2.4, §6).

Run modes:
  cron / manual:        python retrain_worker.py [--force]
  background thread:    from retrain_worker import maybe_retrain; maybe_retrain()

Concurrency-safe with the live trading loop: the new model is written to a temp
file and os.replace()-d onto the production path only after the validation gate
passes — readers either see the old model or the new one, never a partial file.

Deploy gate: validation AUC must be >= min_val_auc_to_deploy (default 0.52).
A model worse than (or indistinguishable from) random never replaces a working
one (spec §2.2.7).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
from ml_engine import FEATURE_NAMES, meta_path, ml_config, model_dir, model_path


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ----------------------------------------------------------------------------- dataset

def build_dataset(window_days: int) -> tuple[list[list[float]], list[int], list[str]]:
    """Reconstruct the training set from closed trades' feature_snapshot JSON
    (spec §4 — this is why the snapshot column is critical). Chronological order
    ascending; trades without a snapshot are skipped."""
    cutoff = (_utcnow() - dt.timedelta(days=window_days)).isoformat()
    rows = db.conn().execute(
        """SELECT ts_close, pnl, feature_snapshot FROM trades
           WHERE status='closed' AND feature_snapshot IS NOT NULL AND ts_close >= ?
           ORDER BY ts_close ASC""", (cutoff,)).fetchall()
    X, y, ts = [], [], []
    for r in rows:
        try:
            feats = json.loads(r["feature_snapshot"])
            X.append([float(feats.get(k, 0.0)) for k in FEATURE_NAMES])
            y.append(1 if (r["pnl"] or 0) > 0 else 0)   # breakeven counts as 0 (spec §2.2.2)
            ts.append(r["ts_close"])
        except Exception:
            continue
    return X, y, ts


def _chrono_split(X, y, frac=0.8):
    cut = max(1, int(len(X) * frac))
    return X[:cut], y[:cut], X[cut:], y[cut:]


def _auc(y_true, y_prob) -> float | None:
    """Validation AUC. sklearn if present, otherwise a rank-based Mann-Whitney
    computation. None when the validation set has only one class."""
    if len(set(y_true)) < 2:
        return None
    try:
        from sklearn.metrics import roc_auc_score
        return float(roc_auc_score(y_true, y_prob))
    except ImportError:
        pairs = sorted(zip(y_prob, y_true))
        ranks, i = {}, 0
        while i < len(pairs):                       # average ranks for ties
            j = i
            while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
                j += 1
            for k in range(i, j + 1):
                ranks[k] = (i + j) / 2 + 1
            i = j + 1
        pos = [ranks[k] for k, (_, t) in enumerate(pairs) if t == 1]
        n1, n0 = len(pos), len(pairs) - len(pos)
        u = sum(pos) - n1 * (n1 + 1) / 2
        return u / (n1 * n0)


def _make_model():
    """Spec §2.2.3 hyperparameters, fixed — no tuning by design."""
    import xgboost as xgb
    params = dict(n_estimators=200, max_depth=4, learning_rate=0.05,
                  subsample=0.8, colsample_bytree=0.8, min_child_weight=3,
                  eval_metric="logloss", random_state=42)
    try:
        return xgb.XGBClassifier(use_label_encoder=False, **params)
    except TypeError:   # kwarg removed in newer xgboost releases
        return xgb.XGBClassifier(**params)


# ----------------------------------------------------------------------------- train + deploy

def retrain(force: bool = False) -> dict:
    cfg = ml_config()
    min_samples = int(cfg.get("min_samples_to_train", 30))
    X, y, ts = build_dataset(int(cfg.get("training_window_days", 90)))

    if len(X) < min_samples:
        msg = f"ml: {len(X)} samples < {min_samples} minimum — staying on rules"
        db.log_event("info", "ml", msg)
        return {"deployed": False, "reason": msg, "n_samples": len(X)}

    try:
        model = _make_model()
    except ImportError:
        db.log_event("warn", "ml", "xgboost not installed — cannot retrain")
        return {"deployed": False, "reason": "xgboost not installed", "n_samples": len(X)}

    Xtr, ytr, Xva, yva = _chrono_split(X, y)        # chronological, never random (§2.2.4)
    model.fit(Xtr, ytr)
    prob = [p[1] for p in model.predict_proba(Xva)]
    auc = _auc(yva, prob)
    acc = (sum(1 for p, t in zip(prob, yva) if (p >= 0.5) == bool(t)) / len(yva)
           if yva else 0.0)

    gate = float(cfg.get("min_val_auc_to_deploy", 0.52))
    if auc is None or auc < gate:
        why = ("validation set has a single class" if auc is None
               else f"val AUC {auc:.3f} < {gate}")
        db.log_event("warn", "ml",
                     f"retrain rejected ({why}) — keeping previous model")
        db.insert_ml_run(_utcnow().isoformat(), len(X), auc or 0.0, acc,
                         _importances(model), str(model_path()), deployed=False)
        return {"deployed": False, "reason": why, "n_samples": len(X), "val_auc": auc}

    # Atomic deploy (spec §6): temp file in the same dir, then rename.
    model_dir().mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=model_dir())
    os.close(fd)
    model.save_model(tmp)
    os.replace(tmp, model_path())
    meta = {"trained_at": _utcnow().isoformat(), "n_samples": len(X),
            "val_auc": round(auc, 4), "val_accuracy": round(acc, 4),
            "feature_names": FEATURE_NAMES,
            "window": [ts[0], ts[-1]] if ts else None}
    fd, tmpm = tempfile.mkstemp(suffix=".json", dir=model_dir())
    os.close(fd)
    Path(tmpm).write_text(json.dumps(meta, indent=1))
    os.replace(tmpm, meta_path())

    db.insert_ml_run(meta["trained_at"], len(X), auc, acc,
                     _importances(model), str(model_path()), deployed=True)
    db.log_event("info", "ml",
                 f"model deployed: {len(X)} samples, val AUC {auc:.3f}, acc {acc:.3f}")
    return {"deployed": True, "n_samples": len(X), "val_auc": auc, "val_accuracy": acc}


def _importances(model) -> str:
    try:
        imp = getattr(model, "feature_importances_", None)
        if imp is None:
            return "{}"
        return json.dumps({n: round(float(v), 5)
                           for n, v in zip(FEATURE_NAMES, imp)})
    except Exception:
        return "{}"


# ----------------------------------------------------------------------------- scheduling

def maybe_retrain(force: bool = False) -> dict:
    """Retrain when forced, when it's the Sunday-00:00-UTC window, or when 20+
    new closed trades accumulated since the last run (spec §2.2.4)."""
    if not ml_config().get("enabled", True):
        return {"deployed": False, "reason": "ml_engine disabled in config"}
    if force:
        return retrain(force=True)

    last = db.latest_ml_run()
    now = _utcnow()
    sunday_window = now.weekday() == 6 and now.hour == 0
    if last is None:
        return retrain()
    new_trades = db.count_closed_since(last["trained_at"])
    threshold = int(ml_config().get("retrain_on_n_new_trades", 20))
    if sunday_window or new_trades >= threshold:
        return retrain()
    return {"deployed": False,
            "reason": f"no trigger (Sunday window: {sunday_window}, "
                      f"new closed trades {new_trades}/{threshold})"}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="retrain regardless of schedule")
    args = ap.parse_args()
    print(json.dumps(maybe_retrain(force=args.force), indent=2, default=str))
