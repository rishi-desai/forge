import React, { useEffect, useState } from "react";
import { api, fmt$, fmtPct, pnlCls } from "../api";
import { Card, Gauge, Stat, TierBadge } from "../components/ui";
import EquityChart from "../components/EquityChart";

export default function Portfolio() {
  const [d, setD] = useState(null);
  useEffect(() => {
    const load = () => api.portfolio().then(setD);
    load();
    const id = setInterval(load, 30_000);
    return () => clearInterval(id);
  }, []);
  if (!d) return <div className="text-mute text-sm">Loading…</div>;

  // Net portfolio Greeks: rough composite until real option marks are wired in.
  const greeks = d.open_positions.reduce((a, p) => ({
    delta: a.delta + (p.direction === "bullish" ? 0.4 : p.direction === "bearish" ? -0.4 : 0) * (p.contracts || 1),
    theta: a.theta + (p.strategy?.includes("condor") || p.strategy?.includes("secured") ? 8 : -6) * (p.contracts || 1),
    vega: a.vega + (p.strategy?.includes("condor") ? -12 : 10) * (p.contracts || 1),
  }), { delta: 0, theta: 0, vega: 0 });

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Stat label="Total P&L" value={fmt$(d.total_pnl)} tone={pnlCls(d.total_pnl)}
              sub={`equity ${fmt$(d.equity)}`} />
        <Stat label="Today's P&L" value={fmt$(d.today_pnl)} tone={pnlCls(d.today_pnl)}
              sub={`cash ${fmt$(d.cash)} · deployed ${fmt$(d.deployed)}`} />
        <Stat label="Win rate" value={fmtPct(d.win_rate)} sub="closed trades" />
        <Stat label="Fear & Greed" value={d.fear_greed != null ? Math.round(d.fear_greed) : "—"}
              tone={d.fear_greed > 60 ? "text-gain" : d.fear_greed < 40 ? "text-loss" : undefined}
              sub={`regime: ${d.regime?.replaceAll("_", " ") ?? "—"}`} />
      </div>

      <Card title="Equity curve">
        <EquityChart curve={d.equity_curve} />
      </Card>

      <div className="grid lg:grid-cols-3 gap-4">
        <Card title="Open positions" className="lg:col-span-2 overflow-auto max-h-96">
          {d.open_positions.length === 0 ? (
            <p className="text-sm text-mute">No open positions. The bot enters when a signal clears the 0.30 strength floor.</p>
          ) : (
            <table className="w-full tbl">
              <thead><tr>
                <th>Symbol</th><th>Strategy</th><th>Size</th><th>DTE</th><th>Expiry</th><th>Conviction</th>
              </tr></thead>
              <tbody>
                {d.open_positions.map((p) => (
                  <tr key={p.id}>
                    <td className="font-mono">{p.symbol}</td>
                    <td>{p.strategy?.replaceAll("_", " ")}</td>
                    <td className="num font-semibold">{fmt$(p.cost)}</td>
                    <td className="num">{p.dte}</td>
                    <td className="num font-mono text-mute">{p.expiry}</td>
                    <td><TierBadge tier={p.override_tier} />{!p.override_tier &&
                      <span className="text-xs text-faint num">{p.signal_strength?.toFixed(2)}</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card title="Net Greeks">
          <div className="space-y-4">
            <Gauge label="Δ Delta" value={greeks.delta} min={-5} max={5} format={(v) => v.toFixed(1)} />
            <Gauge label="Θ Theta / day" value={greeks.theta} min={-60} max={60} format={(v) => fmt$(v)} />
            <Gauge label="V Vega" value={greeks.vega} min={-80} max={80} format={(v) => v.toFixed(0)} />
            <p className="text-[11px] text-faint leading-relaxed">
              Composite estimates; per-leg Greeks populate once the Alpaca options
              data client provides live marks.
            </p>
          </div>
        </Card>
      </div>
    </div>
  );
}
