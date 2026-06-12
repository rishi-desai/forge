import React, { useEffect, useMemo, useState } from "react";
import { api, state, subscribe } from "./api";
import { OverrideModal, Toasts } from "./components/ui";
import Portfolio from "./views/Portfolio";
import Signals from "./views/Signals";
import TradeLog from "./views/TradeLog";
import Flow from "./views/Flow";
import Foreign from "./views/Foreign";
import Learning from "./views/Learning";
import Settings from "./views/Settings";

const VIEWS = [
  ["portfolio", "Portfolio", Portfolio],
  ["signals", "Signals", Signals],
  ["trades", "Trade Log", TradeLog],
  ["flow", "Options Flow", Flow],
  ["foreign", "Overnight", Foreign],
  ["learning", "Learning", Learning],
  ["settings", "Settings", Settings],
];

function useClockET() {
  const [now, setNow] = useState(new Date());
  useEffect(() => { const id = setInterval(() => setNow(new Date()), 1000); return () => clearInterval(id); }, []);
  const et = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(now);
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York",
    weekday: "short", hour: "numeric", minute: "numeric", hour12: false })
    .formatToParts(now).reduce((a, p) => ({ ...a, [p.type]: p.value }), {});
  const mins = parseInt(parts.hour) * 60 + parseInt(parts.minute);
  const weekend = ["Sat", "Sun"].includes(parts.weekday);
  const phase = weekend ? "closed"
    : mins >= 240 && mins < 570 ? "pre-market"
    : mins >= 570 && mins < 960 ? "open"
    : mins >= 960 && mins < 1200 ? "after-hours" : "closed";
  let countdown = "";
  if (phase === "open") {
    const left = 960 - mins;
    countdown = `closes in ${Math.floor(left / 60)}h ${left % 60}m`;
  } else if (phase === "pre-market") {
    const left = 570 - mins;
    countdown = `opens in ${Math.floor(left / 60)}h ${left % 60}m`;
  }
  return { et, phase, countdown };
}

export default function App() {
  const [view, setView] = useState("portfolio");
  const [toasts, setToasts] = useState([]);
  const [pendingOverride, setPendingOverride] = useState(null);
  const [head, setHead] = useState({});
  const { et, phase, countdown } = useClockET();

  const push = (title, body, tone = "gain") =>
    setToasts((t) => [...t, { id: Math.random().toString(36).slice(2), title, body, tone }].slice(-4));
  const dismiss = (id) => setToasts((t) => t.filter((x) => x.id !== id));

  useEffect(() => {
    api.portfolio().then((p) => setHead({ vix: p.vix, regime: p.regime, profile: p.profile,
      regimeDetail: p.regime_detail, regimeConf: p.regime_confidence, regimeSrc: p.regime_source }));
    const unsub = subscribe(({ kind, payload }) => {
      if (kind === "trade_executed")
        push(`Trade executed: ${payload.symbol}`,
             `${payload.strategy?.replaceAll("_", " ")} — $${Math.round(payload.cost).toLocaleString()}` +
             (payload.override ? ` (${payload.override} override)` : ""),
             payload.override ? "watch" : "gain");
      if (kind === "trade_closed")
        push(`Position closed #${payload.trade_id}`,
             `${payload.reason} — P&L $${Math.round(payload.pnl)}`,
             payload.pnl >= 0 ? "gain" : "loss");
      if (kind === "halt") push("Trading halted", payload.reason, "loss");
      if (kind === "override_pending") setPendingOverride(payload);
      if (kind === "override_resolved") setPendingOverride(null);
    });
    return unsub;
  }, []);

  const resolveOverride = async (id, approve) => {
    await api.resolveOverride(id, approve);
    setPendingOverride(null);
    push(approve ? "Override approved" : "Override skipped",
         approve ? "Executing at override size" : "Reverting to normal profile sizing",
         "watch");
  };

  const Active = useMemo(() => VIEWS.find(([k]) => k === view)[2], [view]);
  const vixTone = head.vix > 30 ? "text-loss" : head.vix > 20 ? "text-watch" : "text-gain";

  return (
    <div className="min-h-screen flex">
      <nav className="w-44 shrink-0 border-r border-line bg-well/40 p-3 flex flex-col gap-1">
        <div className="px-2 py-3">
          <div className="text-sm font-semibold tracking-tight">Forge</div>
          <div className="text-[11px] text-faint">paper · cash account</div>
        </div>
        {VIEWS.map(([key, label]) => (
          <button key={key} onClick={() => setView(key)}
            className={`text-left text-sm px-3 py-2 rounded focus:outline-none focus-visible:ring-1 ring-info ${
              view === key ? "bg-panel text-text border border-line" : "text-mute hover:text-text"}`}>
            {label}
          </button>
        ))}
        <div className="mt-auto px-2 pb-1 text-[11px] text-faint">
          profile <span className="text-mute">{head.profile || "—"}</span>
        </div>
      </nav>

      <div className="flex-1 min-w-0">
        <header className="h-12 border-b border-line flex items-center gap-4 px-4 text-sm sticky top-0 bg-ink/90 backdrop-blur z-20">
          <span className="num font-mono text-mute">{et} ET</span>
          <span className={`text-[11px] px-2 py-0.5 rounded-full border ${
            phase === "open" ? "border-gain/50 text-gain" :
            phase === "closed" ? "border-line text-faint" : "border-watch/50 text-watch"}`}>
            {phase}
          </span>
          {countdown && <span className="text-xs text-faint">{countdown}</span>}
          <span className="ml-auto text-xs text-mute">
            VIX <span className={`num font-mono ${vixTone}`}>{head.vix?.toFixed?.(1) ?? "—"}</span>
          </span>
          <span className="text-xs text-mute"
                title={head.regimeSrc === "model"
                  ? `GMM regime model, posterior ${Math.round((head.regimeConf || 0) * 100)}%`
                  : "rule-based (VIX/ADX) classification"}>
            regime <span className="text-text">
              {(head.regimeDetail || head.regime)?.replaceAll("_", " ") ?? "—"}
            </span>
            {head.regimeSrc === "model" && head.regimeConf != null && (
              <span className="num text-faint"> {Math.round(head.regimeConf * 100)}%</span>
            )}
          </span>
          {state.demo && (
            <span className="text-[11px] px-2 py-0.5 rounded border border-info/40 text-info">
              demo data — backend offline
            </span>
          )}
        </header>
        <main className="p-4 max-w-[1440px]">
          <Active push={push} />
        </main>
      </div>

      <Toasts items={toasts} dismiss={dismiss} />
      <OverrideModal pending={pendingOverride} onResolve={resolveOverride} />
    </div>
  );
}
