"""
db.py — Storage layer. SQLite by default ($0, zero-config). Every consumer goes
through this module's functions, so swapping to Supabase Postgres is a matter of
re-implementing these ~15 functions against supabase-py (see README "Supabase
swap"); the schema below maps 1:1 to Postgres tables.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import threading
from typing import Optional

_LOCAL = threading.local()
_DB_PATH = os.environ.get("DB_PATH", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "trading.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_open TEXT NOT NULL, ts_close TEXT,
  symbol TEXT NOT NULL, strategy TEXT NOT NULL, strategy_type TEXT NOT NULL,
  direction TEXT, contracts INTEGER, legs_json TEXT,
  entry_price REAL, exit_price REAL, cost REAL, collateral REAL,
  max_profit REAL, max_loss REAL, pnl REAL, status TEXT DEFAULT 'open',
  signal_strength REAL, iv_rank REAL, market_regime TEXT,
  risk_profile TEXT, sized_dollars REAL, rationale TEXT,
  override_tier TEXT, override_normal_size REAL, override_approved_by TEXT,
  dte INTEGER, expiry TEXT, catalyst TEXT
);
CREATE TABLE IF NOT EXISTS trade_signals (
  trade_id INTEGER, name TEXT, category TEXT, direction TEXT,
  strength REAL, detail TEXT, was_correct INTEGER
);
CREATE TABLE IF NOT EXISTS signal_weights (
  name TEXT PRIMARY KEY, weight REAL DEFAULT 1.0, samples INTEGER DEFAULT 0,
  correct INTEGER DEFAULT 0, updated TEXT
);
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER, ts TEXT,
  strategy TEXT, signal_accuracy REAL, what_worked TEXT, what_failed TEXT,
  market_context TEXT
);
CREATE TABLE IF NOT EXISTS overrides_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, trade_id INTEGER,
  tier TEXT, criteria_json TEXT, criteria_met INTEGER,
  normal_size REAL, override_size REAL, auto_executed INTEGER, outcome TEXT
);
CREATE TABLE IF NOT EXISTS rejected_overrides (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, tier TEXT,
  criteria_met INTEGER, reason TEXT, signal_strength REAL
);
CREATE TABLE IF NOT EXISTS equity_curve (
  ts TEXT PRIMARY KEY, equity REAL, cash REAL, deployed REAL
);
CREATE TABLE IF NOT EXISTS weekly_reports (
  week_ending TEXT PRIMARY KEY, report_json TEXT
);
CREATE TABLE IF NOT EXISTS live_signals (
  ts TEXT, symbol TEXT, name TEXT, direction TEXT, strength REAL,
  suggested_strategy TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS events (
  ts TEXT, level TEXT, kind TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS ml_model_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trained_at TIMESTAMP NOT NULL,
  n_samples INTEGER NOT NULL,
  val_auc FLOAT NOT NULL,
  val_accuracy FLOAT NOT NULL,
  feature_importances TEXT,
  model_path TEXT NOT NULL,
  deployed INTEGER DEFAULT 1
);
"""

# Additive migrations for existing databases (ALTER ADD COLUMN is idempotent
# via the PRAGMA check; spec §4 — ml columns on the trades table).
_TRADES_ML_COLUMNS = [
    ("scoring_method", "TEXT DEFAULT 'rules'"),
    ("ml_score", "FLOAT"),
    ("feature_snapshot", "TEXT"),
    # entry_iv: annualized vol used to price the legs at entry, so manage_positions
    # can reprice a live mark (Black-Scholes) instead of holding at cost.
    ("entry_iv", "REAL"),
]


def _migrate(c: sqlite3.Connection):
    have = {r[1] for r in c.execute("PRAGMA table_info(trades)")}
    for name, decl in _TRADES_ML_COLUMNS:
        if name not in have:
            c.execute(f"ALTER TABLE trades ADD COLUMN {name} {decl}")
    c.commit()


def conn() -> sqlite3.Connection:
    c = getattr(_LOCAL, "conn", None)
    if c is None:
        os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
        c = sqlite3.connect(_DB_PATH)
        c.row_factory = sqlite3.Row
        c.executescript(SCHEMA)
        _migrate(c)
        _LOCAL.conn = c
    return c


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ----------------------------------------------------------------------------- trades

def insert_trade(cand, contracts: int, cost: float, sized: float, profile: str,
                 regime: str, rationale: str, override=None) -> int:
    c = conn()
    cur = c.execute(
        """INSERT INTO trades (ts_open, symbol, strategy, strategy_type, direction,
           contracts, legs_json, entry_price, cost, collateral, max_profit, max_loss,
           signal_strength, iv_rank, market_regime, risk_profile, sized_dollars,
           rationale, override_tier, override_normal_size, dte, expiry, catalyst,
           scoring_method, ml_score, feature_snapshot, entry_iv)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (_now(), cand.symbol, cand.strategy, cand.strategy_type, cand.direction,
         contracts, json.dumps([l.__dict__ for l in cand.legs], default=str),
         cand.est_debit or -cand.est_credit, cost,
         cand.collateral_per_contract * contracts,
         cand.max_profit * contracts, cand.max_loss * contracts,
         cand.signal_strength, cand.iv_rank, regime, profile, sized, rationale,
         override.tier if override else None,
         override.normal_size if override else None,
         cand.dte, str(cand.expiry), cand.catalyst,
         getattr(cand, "scoring_method", "rules"),
         getattr(cand, "ml_score", None),
         getattr(cand, "feature_snapshot", None),
         getattr(cand, "entry_iv", None)))
    trade_id = cur.lastrowid
    for s in cand.signals:
        c.execute("INSERT INTO trade_signals VALUES (?,?,?,?,?,?,NULL)",
                  (trade_id, s.name, s.category, s.direction, s.strength, s.detail))
    if override:
        c.execute("""INSERT INTO overrides_log (ts, trade_id, tier, criteria_json,
                     criteria_met, normal_size, override_size, auto_executed)
                     VALUES (?,?,?,?,?,?,?,?)""",
                  (_now(), trade_id, override.tier, json.dumps(override.criteria),
                   override.criteria_met, override.normal_size, sized,
                   0 if override.requires_approval else 1))
    c.commit()
    return trade_id


def close_trade(trade_id: int, exit_price: float, pnl: float):
    c = conn()
    c.execute("UPDATE trades SET ts_close=?, exit_price=?, pnl=?, status='closed' WHERE id=?",
              (_now(), exit_price, pnl, trade_id))
    c.commit()


def open_trades() -> list[dict]:
    return [dict(r) for r in conn().execute(
        "SELECT * FROM trades WHERE status='open' ORDER BY ts_open DESC")]


def all_trades(limit: int = 500) -> list[dict]:
    return [dict(r) for r in conn().execute(
        "SELECT * FROM trades ORDER BY ts_open DESC LIMIT ?", (limit,))]


def trade_signal_rows(trade_id: int) -> list[dict]:
    return [dict(r) for r in conn().execute(
        "SELECT * FROM trade_signals WHERE trade_id=?", (trade_id,))]


# ----------------------------------------------------------------------------- weights & lessons

def get_signal_weights() -> dict:
    return {r["name"]: r["weight"] for r in conn().execute("SELECT * FROM signal_weights")}


def get_signal_samples() -> dict:
    """Per-signal observation counts, used to anneal the learning rate."""
    return {r["name"]: r["samples"]
            for r in conn().execute("SELECT name, samples FROM signal_weights")}


def update_signal_weight(name: str, weight: float, was_correct: bool):
    conn().execute(
        """INSERT INTO signal_weights (name, weight, samples, correct, updated)
           VALUES (?,?,1,?,?)
           ON CONFLICT(name) DO UPDATE SET weight=?, samples=samples+1,
           correct=correct+?, updated=?""",
        (name, weight, int(was_correct), _now(), weight, int(was_correct), _now()))
    conn().commit()


def insert_lesson(trade_id: int, strategy: str, accuracy: float,
                  worked: list, failed: list, regime: str):
    conn().execute(
        "INSERT INTO lessons (trade_id, ts, strategy, signal_accuracy, what_worked, "
        "what_failed, market_context) VALUES (?,?,?,?,?,?,?)",
        (trade_id, _now(), strategy, accuracy, json.dumps(worked),
         json.dumps(failed), regime))
    conn().commit()


def mark_signal_outcomes(trade_id: int, correct_names: set):
    c = conn()
    for r in trade_signal_rows(trade_id):
        c.execute("UPDATE trade_signals SET was_correct=? WHERE trade_id=? AND name=?",
                  (int(r["name"] in correct_names), trade_id, r["name"]))
    c.commit()


# ----------------------------------------------------------------------------- misc

def snapshot_equity(equity: float, cash: float, deployed: float):
    conn().execute("INSERT OR REPLACE INTO equity_curve VALUES (?,?,?,?)",
                   (_now(), equity, cash, deployed))
    conn().commit()


def equity_curve(limit: int = 2000) -> list[dict]:
    return [dict(r) for r in conn().execute(
        "SELECT * FROM equity_curve ORDER BY ts DESC LIMIT ?", (limit,))][::-1]


def first_equity_on_or_after(date_iso: str) -> Optional[float]:
    """Earliest recorded equity at/after a date (YYYY-MM-DD). Used to recover the
    week's starting equity across restarts so the weekly-loss guard survives a
    process bounce instead of resetting to today's equity each morning."""
    r = conn().execute(
        "SELECT equity FROM equity_curve WHERE ts >= ? ORDER BY ts ASC LIMIT 1",
        (date_iso,)).fetchone()
    return float(r["equity"]) if r and r["equity"] is not None else None


def record_live_signal(symbol: str, s, suggested: Optional[str]):
    conn().execute("INSERT INTO live_signals VALUES (?,?,?,?,?,?,?)",
                   (_now(), symbol, s.name, s.direction, s.strength,
                    suggested or s.strategy_hint, s.detail))
    conn().commit()


def recent_live_signals(limit: int = 100) -> list[dict]:
    return [dict(r) for r in conn().execute(
        "SELECT * FROM live_signals ORDER BY ts DESC LIMIT ?", (limit,))]


def log_event(level: str, kind: str, message: str):
    conn().execute("INSERT INTO events VALUES (?,?,?,?)", (_now(), level, kind, message))
    conn().commit()


def log_rejected_override(rec: dict):
    conn().execute(
        "INSERT INTO rejected_overrides (ts, symbol, tier, criteria_met, reason, "
        "signal_strength) VALUES (?,?,?,?,?,?)",
        (rec["ts"], rec["symbol"], rec["tier"], rec["criteria_met"], rec["reason"],
         rec["signal_strength"]))
    conn().commit()


def save_weekly_report(week_ending: str, report: dict):
    conn().execute("INSERT OR REPLACE INTO weekly_reports VALUES (?,?)",
                   (week_ending, json.dumps(report)))
    conn().commit()


def weekly_reports(limit: int = 12) -> list[dict]:
    return [{"week_ending": r["week_ending"], **json.loads(r["report_json"])}
            for r in conn().execute(
                "SELECT * FROM weekly_reports ORDER BY week_ending DESC LIMIT ?", (limit,))]


def override_stats() -> dict:
    c = conn()
    norm = c.execute("""SELECT COUNT(*) n, AVG(pnl) avg_pnl,
                        AVG(CASE WHEN pnl>0 THEN 1.0 ELSE 0.0 END) wr
                        FROM trades WHERE status='closed' AND override_tier IS NULL""").fetchone()
    ovr = c.execute("""SELECT COUNT(*) n, AVG(pnl) avg_pnl,
                       AVG(CASE WHEN pnl>0 THEN 1.0 ELSE 0.0 END) wr
                       FROM trades WHERE status='closed' AND override_tier IS NOT NULL""").fetchone()
    return {"normal": dict(norm), "override": dict(ovr)}


# ----------------------------------------------------------------------------- ml engine

def insert_ml_run(trained_at: str, n_samples: int, val_auc: float, val_accuracy: float,
                  feature_importances: str, model_path_str: str, deployed: bool = True):
    conn().execute(
        """INSERT INTO ml_model_runs (trained_at, n_samples, val_auc, val_accuracy,
           feature_importances, model_path, deployed) VALUES (?,?,?,?,?,?,?)""",
        (trained_at, n_samples, val_auc, val_accuracy, feature_importances,
         model_path_str, int(deployed)))
    conn().commit()


def latest_ml_run() -> Optional[dict]:
    r = conn().execute(
        "SELECT * FROM ml_model_runs WHERE deployed=1 ORDER BY trained_at DESC LIMIT 1"
    ).fetchone()
    return dict(r) if r else None


def ml_runs(limit: int = 12) -> list[dict]:
    return [dict(r) for r in conn().execute(
        "SELECT * FROM ml_model_runs ORDER BY trained_at DESC LIMIT ?", (limit,))]


def count_closed_since(ts: str) -> int:
    r = conn().execute(
        "SELECT COUNT(*) n FROM trades WHERE status='closed' AND ts_close >= ?",
        (ts,)).fetchone()
    return r["n"] if r else 0


def scoring_method_stats() -> dict:
    """ML vs rules realized performance for the dashboard comparison panel."""
    out = {}
    for method in ("ml", "rules"):
        r = conn().execute(
            """SELECT COUNT(*) n, AVG(pnl) avg_pnl,
               AVG(CASE WHEN pnl>0 THEN 1.0 ELSE 0.0 END) wr
               FROM trades WHERE status='closed' AND scoring_method=?""",
            (method,)).fetchone()
        out[method] = dict(r)
    return out
