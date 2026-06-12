// ui.jsx — shared primitives. The ConvictionMeter is the dashboard's signature
// element: the 7 override criteria as a segmented bar, amber-lit when met.
import React, { useEffect, useState } from "react";
import { fmt$, pnlCls } from "../api";

export const Card = ({ title, right, children, className = "" }) => (
  <section className={`card p-4 ${className}`}>
    {(title || right) && (
      <header className="flex items-center justify-between mb-3">
        <h2 className="lbl">{title}</h2>
        {right}
      </header>
    )}
    {children}
  </section>
);

export const Stat = ({ label, value, sub, tone }) => (
  <div className="card p-4">
    <div className="lbl mb-1">{label}</div>
    <div className={`num text-2xl font-semibold ${tone || "text-text"}`}>{value}</div>
    {sub && <div className="text-xs text-mute mt-1">{sub}</div>}
  </div>
);

export const DirBadge = ({ d }) => {
  const map = { bullish: "text-gain border-gain/40", bearish: "text-loss border-loss/40",
                neutral: "text-mute border-line" };
  return <span className={`text-[11px] px-1.5 py-0.5 rounded border ${map[d] || map.neutral}`}>{d}</span>;
};

const BOLTS = { STRONG: "⚡", MAX: "⚡⚡", ALL_IN: "⚡⚡⚡" };
export const TierBadge = ({ tier }) =>
  tier ? (
    <span className="text-[11px] px-1.5 py-0.5 rounded border border-watch/50 text-watch bg-watch/10 whitespace-nowrap">
      {BOLTS[tier]} {tier.replace("_", "-")}
    </span>
  ) : null;

export const StrengthBar = ({ v }) => (
  <div className="w-24 h-1.5 bg-well rounded overflow-hidden">
    <div className="h-full bg-info rounded" style={{ width: `${Math.round(v * 100)}%` }} />
  </div>
);

const CRITERIA = [
  ["full_signal_alignment", "Signal alignment"],
  ["institutional_options_flow", "Institutional flow"],
  ["favorable_risk_reward", "R:R ≥ 3:1"],
  ["catalyst_present", "Catalyst"],
  ["favorable_iv", "IV favorable"],
  ["benign_market_regime", "Benign regime"],
  ["strong_historical_analog", "Historical analog"],
];

export const ConvictionMeter = ({ criteria = {}, compact = false }) => (
  <div>
    <div className="flex gap-1">
      {CRITERIA.map(([k, label]) => (
        <div key={k} title={label}
             className={`h-2 flex-1 rounded-sm ${criteria[k] ? "bg-watch" : "bg-well border border-line"}`} />
      ))}
    </div>
    {!compact && (
      <div className="grid grid-cols-2 gap-x-4 mt-2">
        {CRITERIA.map(([k, label]) => (
          <div key={k} className="flex items-center gap-1.5 text-xs py-0.5">
            <span className={criteria[k] ? "text-watch" : "text-faint"}>{criteria[k] ? "✓" : "✕"}</span>
            <span className={criteria[k] ? "text-text" : "text-faint"}>{label}</span>
          </div>
        ))}
      </div>
    )}
  </div>
);

export const Gauge = ({ label, value, min, max, format = (v) => v }) => {
  const pct = Math.min(Math.max(((value ?? 0) - min) / (max - min), 0), 1) * 100;
  const tone = value > 0 ? "bg-gain" : value < 0 ? "bg-loss" : "bg-mute";
  return (
    <div>
      <div className="flex justify-between text-xs mb-1">
        <span className="text-mute">{label}</span>
        <span className={`num font-mono ${pnlCls(value)}`}>{format(value)}</span>
      </div>
      <div className="h-1.5 bg-well rounded relative overflow-hidden">
        <div className="absolute left-1/2 top-0 bottom-0 w-px bg-line" />
        <div className={`h-full ${tone} rounded`} style={{
          marginLeft: value >= 0 ? "50%" : `${pct}%`,
          width: `${Math.abs(pct - 50)}%` }} />
      </div>
    </div>
  );
};

// ----------------------------------------------------------------------- toasts

export function Toasts({ items, dismiss }) {
  return (
    <div className="fixed top-4 right-4 z-50 space-y-2 w-80">
      {items.map((t) => (
        <div key={t.id}
             className={`card p-3 text-sm shadow-lg border-l-2 ${
               t.tone === "loss" ? "border-l-loss" : t.tone === "watch" ? "border-l-watch" : "border-l-gain"}`}>
          <div className="flex justify-between gap-2">
            <div>
              <div className="font-medium">{t.title}</div>
              {t.body && <div className="text-mute text-xs mt-0.5">{t.body}</div>}
            </div>
            <button onClick={() => dismiss(t.id)} className="text-faint hover:text-text">✕</button>
          </div>
        </div>
      ))}
    </div>
  );
}

// ------------------------------------------------------------ ALL-IN approval modal

export function OverrideModal({ pending, onResolve }) {
  const [left, setLeft] = useState(0);
  useEffect(() => {
    if (!pending) return;
    const tick = () => setLeft(Math.max(0,
      Math.round((new Date(pending.expires_at) - Date.now()) / 1000)));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [pending]);
  if (!pending) return null;
  return (
    <div className="fixed inset-0 z-50 bg-ink/80 flex items-center justify-center p-4">
      <div className="card max-w-md w-full p-5 border-watch/40">
        <div className="flex items-center justify-between mb-1">
          <TierBadge tier={pending.tier} />
          <span className="num font-mono text-xs text-mute">
            reverts to normal size in {Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")}
          </span>
        </div>
        <h2 className="text-lg font-semibold mt-2">
          The model wants to go heavy on <span className="font-mono">{pending.symbol}</span>
        </h2>
        <p className="text-sm text-mute mt-1">
          {pending.strategy?.replaceAll("_", " ")} — normal size {fmt$(pending.normal_size)},
          requested up to 50% of available cash (capped at $10,000 universal limit).
        </p>
        <div className="mt-4"><ConvictionMeter criteria={pending.criteria} /></div>
        <div className="flex gap-2 mt-5">
          <button onClick={() => onResolve(pending.approval_id, true)}
                  className="flex-1 bg-watch text-ink font-medium rounded px-3 py-2 hover:opacity-90">
            Execute at override size
          </button>
          <button onClick={() => onResolve(pending.approval_id, false)}
                  className="flex-1 border border-line rounded px-3 py-2 text-mute hover:text-text">
            Skip — use normal size
          </button>
        </div>
      </div>
    </div>
  );
}
