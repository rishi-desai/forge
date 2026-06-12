// api.js — talks to the FastAPI backend; falls back to mock data so the
// dashboard is fully explorable before any keys are configured (demo mode).
import { MOCK } from "./mock";

const BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";
export const state = { demo: false };

async function get(path, mockKey) {
  try {
    const r = await fetch(`${BASE}${path}`, { signal: AbortSignal.timeout(3500) });
    if (!r.ok) throw new Error(r.status);
    state.demo = false;
    return await r.json();
  } catch {
    state.demo = true;
    return MOCK[mockKey];
  }
}

export const api = {
  portfolio: () => get("/api/portfolio", "portfolio"),
  signals:   () => get("/api/signals", "signals"),
  trades:    () => get("/api/trades", "trades"),
  overrides: () => get("/api/overrides", "overrides"),
  foreign:   () => get("/api/foreign", "foreign"),
  learning:  () => get("/api/learning", "learning"),
  settings:  () => get("/api/settings", "settings"),

  saveSettings: async (body) => {
    try {
      const r = await fetch(`${BASE}/api/settings`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      return await r.json();
    } catch { return { ok: false, demo: true }; }
  },
  resolveOverride: async (id, approve) => {
    try {
      const r = await fetch(`${BASE}/api/overrides/${id}/${approve ? "approve" : "skip"}`,
        { method: "POST" });
      return await r.json();
    } catch { return { ok: false, demo: true }; }
  },
};

// WebSocket push: trade_executed / trade_closed / override_pending / halt / foreign
export function subscribe(onEvent) {
  let ws, alive = true;
  const connect = () => {
    try {
      ws = new WebSocket(BASE.replace(/^http/, "ws") + "/ws");
      ws.onmessage = (e) => { try { onEvent(JSON.parse(e.data)); } catch {} };
      ws.onclose = () => { if (alive) setTimeout(connect, 5000); };
    } catch { if (alive) setTimeout(connect, 5000); }
  };
  connect();
  return () => { alive = false; ws && ws.close(); };
}

export const fmt$ = (v, d = 0) =>
  v == null ? "—" : (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US",
    { minimumFractionDigits: d, maximumFractionDigits: d });
export const fmtPct = (v, d = 1) => (v == null ? "—" : (v * 100).toFixed(d) + "%");
export const pnlCls = (v) => (v > 0 ? "text-gain" : v < 0 ? "text-loss" : "text-mute");
