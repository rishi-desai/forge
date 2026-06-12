"""
app.py — FastAPI backend serving the dashboard + WebSocket push.

Run:  uvicorn app:app --reload --port 8000          (dashboard only)
      RUN_BOT=1 uvicorn app:app --port 8000          (dashboard + live bot loop)

CORS is open to localhost:5173 (Vite dev) and your Vercel domain (set
FRONTEND_ORIGIN). The bot loop runs in a daemon thread; the API stays responsive.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import db
import learning
from data_clients import fear_greed_index
from orchestrator import Orchestrator
from overrides import OVERRIDE_TIERS
from risk import PROFILES, UNIVERSAL_HARD_CAPS

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"

app = FastAPI(title="AI Trading System", version="1.0")
_extra_origins = [o.strip() for o in os.environ.get("FRONTEND_ORIGIN", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"] + _extra_origins,
    allow_methods=["*"], allow_headers=["*"],
)

# ----------------------------------------------------------------------------- ws hub

class Hub:
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.add(ws)

    def push(self, kind: str, payload: dict):
        """Thread-safe push from the bot thread."""
        if not self.loop:
            return
        msg = json.dumps({"kind": kind, "payload": payload}, default=str)
        for ws in list(self.clients):
            asyncio.run_coroutine_threadsafe(self._send(ws, msg), self.loop)

    async def _send(self, ws: WebSocket, msg: str):
        try:
            await ws.send_text(msg)
        except Exception:
            self.clients.discard(ws)


hub = Hub()
_bot: Orchestrator | None = None
_pending_payloads: dict = {}  # approval_id → candidate context for execution


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


@app.on_event("startup")
async def startup():
    hub.loop = asyncio.get_running_loop()
    if os.environ.get("RUN_BOT") == "1":
        global _bot
        cfg = load_config()

        def on_event(kind, payload):
            if kind == "override_pending":
                _pending_payloads[payload["approval_id"]] = payload
            hub.push(kind, payload)

        _bot = Orchestrator(cfg, on_event=on_event)
        threading.Thread(target=_bot.run_forever, daemon=True, name="bot").start()


# ----------------------------------------------------------------------------- views

@app.get("/api/portfolio")
def portfolio():
    opens = db.open_trades()
    deployed = sum(t["cost"] or 0 for t in opens)
    cash = _bot.ledger.available() if _bot else None
    curve = db.equity_curve()
    closed = [t for t in db.all_trades(1000) if t["status"] == "closed"]
    wins = [t for t in closed if (t["pnl"] or 0) > 0]
    return {
        "cash": cash, "deployed": deployed,
        "equity": (cash + deployed) if cash is not None else
                  (curve[-1]["equity"] if curve else None),
        "total_pnl": sum(t["pnl"] or 0 for t in closed),
        "today_pnl": sum(t["pnl"] or 0 for t in closed
                         if (t["ts_close"] or "")[:10] == curve[-1]["ts"][:10]) if curve else 0,
        "win_rate": round(len(wins) / len(closed), 3) if closed else None,
        "open_positions": opens,
        "equity_curve": curve,
        "regime": _bot.ctx.regime if _bot else None,
        "regime_detail": _bot.ctx.regime_detail if _bot else None,
        "regime_confidence": _bot.ctx.regime_confidence if _bot else None,
        "regime_source": _bot.ctx.regime_source if _bot else None,
        "vix": _bot.ctx.vix if _bot else None,
        "fear_greed": fear_greed_index(),
        "profile": load_config()["risk"]["profile"],
    }


@app.get("/api/signals")
def signals():
    return {"signals": db.recent_live_signals(150),
            "regime": _bot.ctx.regime if _bot else None}


@app.get("/api/trades")
def trades(limit: int = 300):
    return {"trades": db.all_trades(limit)}


@app.get("/api/trades/{trade_id}/signals")
def trade_signals(trade_id: int):
    return {"signals": db.trade_signal_rows(trade_id)}


@app.get("/api/overrides")
def overrides_view():
    rows = [t for t in db.all_trades(500) if t["override_tier"]]
    return {"override_trades": rows, "stats": db.override_stats(),
            "pending": list(_pending_payloads.values()),
            "tiers": OVERRIDE_TIERS,
            "rejected": [dict(r) for r in db.conn().execute(
                "SELECT * FROM rejected_overrides ORDER BY ts DESC LIMIT 50")]}


@app.get("/api/foreign")
def foreign():
    if _bot:
        return {"summary": _bot.ctx.overnight_summary, "bias": _bot.ctx.overnight_bias}
    return {"summary": "", "bias": "neutral"}


@app.get("/api/learning")
def learning_view():
    import json as _json
    try:
        import ml_engine
        ml_status = ml_engine.status()
    except Exception:
        ml_status = {"enabled": False, "available": False, "scoring_method_current": "rules"}
    runs = db.ml_runs()
    importances = {}
    if runs:
        try:
            importances = dict(sorted(
                _json.loads(runs[0].get("feature_importances") or "{}").items(),
                key=lambda kv: -kv[1])[:5])
        except Exception:
            pass
    try:
        from regime_model import RegimeModel
        regime_status = (_bot.regime_model.status() if _bot else RegimeModel().status())
    except Exception:
        regime_status = {"enabled": False, "available": False}
    return {"weekly_reports": db.weekly_reports(),
            "regime": regime_status,
            "signal_weights": [dict(r) for r in db.conn().execute(
                "SELECT * FROM signal_weights ORDER BY weight DESC")],
            "lessons": [dict(r) for r in db.conn().execute(
                "SELECT * FROM lessons ORDER BY ts DESC LIMIT 50")],
            "ml": {**ml_status, "runs": runs, "top_features": importances,
                   "method_stats": db.scoring_method_stats()}}


@app.post("/api/ml/retrain")
def force_retrain():
    """Manual retrain trigger (also runs Sundays / every 20 closed trades)."""
    from retrain_worker import maybe_retrain
    return maybe_retrain(force=True)


# ----------------------------------------------------------------------------- settings

class SettingsUpdate(BaseModel):
    starting_cash: float | None = None
    profile: str | None = None
    custom_overrides: dict | None = None
    conviction_overrides: dict | None = None


@app.get("/api/settings")
def get_settings():
    cfg = load_config()
    return {"config": cfg, "profiles": PROFILES, "hard_caps": UNIVERSAL_HARD_CAPS,
            "tiers": OVERRIDE_TIERS}


@app.put("/api/settings")
def put_settings(u: SettingsUpdate):
    cfg = load_config()
    if u.profile:
        if u.profile not in PROFILES:
            raise HTTPException(400, f"Unknown profile {u.profile}")
        cfg["risk"]["profile"] = u.profile
    if u.starting_cash:
        cfg["account"]["starting_cash"] = u.starting_cash
    if u.custom_overrides is not None:
        cfg["risk"]["custom_overrides"] = u.custom_overrides
    if u.conviction_overrides:
        cfg["conviction_overrides"].update(u.conviction_overrides)
    save_config(cfg)
    db.log_event("info", "settings", f"Updated: {u.model_dump(exclude_none=True)}")
    # New sizing rules apply to FUTURE trades only (spec §5.7) — existing
    # positions are untouched. Restart the bot (or it re-reads at next day roll).
    return {"ok": True, "applies_to": "future trades only",
            "restart_required_for": ["profile", "starting_cash"]}


# ----------------------------------------------------------------------------- override approval

@app.post("/api/overrides/{approval_id}/approve")
def approve_override(approval_id: str):
    if not _bot:
        raise HTTPException(409, "Bot not running")
    ov = _bot.resolve_pending_override(approval_id, True)
    payload = _pending_payloads.pop(approval_id, None)
    if not ov or not payload:
        raise HTTPException(404, "Approval not found or expired")
    db.log_event("info", "override_approved", f"{ov.tier} {payload['symbol']} approved by user")
    hub.push("override_resolved", {"approval_id": approval_id, "approved": True})
    return {"ok": True}


@app.post("/api/overrides/{approval_id}/skip")
def skip_override(approval_id: str):
    if _bot:
        _bot.resolve_pending_override(approval_id, False)
    _pending_payloads.pop(approval_id, None)
    hub.push("override_resolved", {"approval_id": approval_id, "approved": False})
    return {"ok": True}


@app.post("/api/reports/weekly/run")
def run_weekly_report():
    import datetime as dt
    return learning.generate_weekly_report(dt.date.today())


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await hub.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keepalive pings from client
    except WebSocketDisconnect:
        hub.clients.discard(ws)
