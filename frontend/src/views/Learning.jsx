import React, { useEffect, useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, fmt$, fmtPct, pnlCls } from "../api";
import { Card } from "../components/ui";

export default function Learning() {
  const [d, setD] = useState(null);
  const [trades, setTrades] = useState([]);
  useEffect(() => {
    api.learning().then(setD);
    api.trades().then((r) => setTrades(r.trades || []));
  }, []);

  const byStrategy = useMemo(() => {
    const m = {};
    for (const t of trades.filter((t) => t.status === "closed")) {
      const b = (m[t.strategy] ||= { strategy: t.strategy, pnl: 0, n: 0, wins: 0 });
      b.pnl += t.pnl || 0; b.n += 1; b.wins += (t.pnl || 0) > 0 ? 1 : 0;
    }
    return Object.values(m).sort((a, b) => b.pnl - a.pnl);
  }, [trades]);

  if (!d) return <div className="text-mute text-sm">Loading…</div>;
  const report = d.weekly_reports?.[0];
  const ml = d.ml || {};

  return (
    <div className="space-y-4">
      <ModelPanel ml={ml} />
      <div className="grid lg:grid-cols-2 gap-4">
        <Card title="P&L by strategy (closed trades)">
          {byStrategy.length === 0 ? (
            <p className="text-sm text-mute">No closed trades yet.</p>
          ) : (
            <ResponsiveContainer width="100%" height={240}>
              <BarChart data={byStrategy} margin={{ top: 8, right: 8, left: 8, bottom: 8 }}>
                <CartesianGrid stroke="#1F2A40" vertical={false} />
                <XAxis dataKey="strategy" tick={{ fill: "#8A99B5", fontSize: 11 }}
                       tickFormatter={(v) => v.replaceAll("_", " ")} interval={0} angle={-12} dy={8} />
                <YAxis tick={{ fill: "#8A99B5", fontSize: 11 }} tickFormatter={(v) => `$${v}`} />
                <Tooltip cursor={{ fill: "#0F1522" }}
                  contentStyle={{ background: "#131A2A", border: "1px solid #1F2A40", borderRadius: 8, fontSize: 12 }}
                  formatter={(v, _, p) => [`${fmt$(v)} · ${p.payload.wins}/${p.payload.n} wins`, "P&L"]}
                  labelFormatter={(l) => l.replaceAll("_", " ")} />
                <Bar dataKey="pnl" radius={[3, 3, 0, 0]}>
                  {byStrategy.map((s, i) => (
                    <Cell key={i} fill={s.pnl >= 0 ? "#34D399" : "#F87171"} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          )}
        </Card>

        <Card title={`Weekly report${report ? ` — week ending ${report.week_ending}` : ""}`}
              right={<span className="text-[11px] text-faint">auto-generated Fri 4:30pm ET</span>}>
          {!report ? (
            <p className="text-sm text-mute">First report generates after a week of trading.</p>
          ) : (
            <div className="space-y-3 text-sm">
              <div className="grid grid-cols-3 gap-3">
                <Mini label="Net P&L" value={fmt$(report.net_pnl)} tone={pnlCls(report.net_pnl)} />
                <Mini label="Win rate" value={fmtPct(report.win_rate)} />
                <Mini label="Sharpe" value={report.sharpe ?? "—"} />
                <Mini label="Trades" value={report.total_trades} />
                <Mini label="Max DD" value={fmtPct(report.max_drawdown)} tone="text-loss" />
                <Mini label="vs SPY" value={report.vs_spy != null ? fmtPct(report.vs_spy) : "—"}
                      tone={pnlCls(report.vs_spy)} />
              </div>
              {report.best_strategy && (
                <p className="text-mute">
                  <span className="text-gain">Best:</span> {report.best_strategy.name?.replaceAll("_", " ")}{" "}
                  ({report.best_strategy.wins}/{report.best_strategy.n}, {fmt$(report.best_strategy.pnl)})
                </p>
              )}
              {report.worst_strategy && (
                <p className="text-mute">
                  <span className="text-loss">Worst:</span> {report.worst_strategy.name?.replaceAll("_", " ")}{" "}
                  ({fmt$(report.worst_strategy.pnl)}) — <span className="text-faint">{report.worst_strategy.root_cause}</span>
                </p>
              )}
              {(report.recommended_adjustments || []).length > 0 && (
                <ul className="text-xs text-mute list-disc ml-4 space-y-1">
                  {report.recommended_adjustments.map((r, i) => <li key={i}>{r}</li>)}
                </ul>
              )}
            </div>
          )}
        </Card>
      </div>

      <Card title="Signal weights (the bot's learned trust per signal)">
        <table className="w-full tbl">
          <thead><tr><th>Signal</th><th>Weight</th><th></th><th>Samples</th><th>Hit rate</th></tr></thead>
          <tbody>
            {(d.signal_weights || []).map((w) => (
              <tr key={w.name}>
                <td>{w.name.replaceAll("_", " ")}</td>
                <td className={`num font-semibold ${w.weight > 1.05 ? "text-gain" : w.weight < 0.9 ? "text-loss" : "text-mute"}`}>
                  {w.weight.toFixed(2)}×
                </td>
                <td className="w-40">
                  <div className="h-1.5 bg-well rounded relative overflow-hidden">
                    <div className="absolute left-1/2 top-0 bottom-0 w-px bg-line" />
                    <div className={`h-full rounded ${w.weight >= 1 ? "bg-gain" : "bg-loss"}`}
                         style={{ marginLeft: w.weight >= 1 ? "50%" : `${(w.weight - 0.25) / 1.75 * 100}%`,
                                  width: `${Math.abs((w.weight - 1) / 1.75) * 100}%` }} />
                  </div>
                </td>
                <td className="num text-mute">{w.samples}</td>
                <td className="num text-mute">{w.samples ? fmtPct(w.correct / w.samples, 0) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="text-[11px] text-faint mt-2">
          Weights start at 1.00× and move with each closed trade (clamped 0.25–2.00×).
          A weight is the multiplier applied to that signal's strength in the composite score.
        </p>
      </Card>
    </div>
  );
}

const Mini = ({ label, value, tone }) => (
  <div className="bg-well rounded p-2">
    <div className="lbl">{label}</div>
    <div className={`num text-base font-semibold ${tone || ""}`}>{value}</div>
  </div>
);

// Model Performance — read-only (ML upgrade spec §7). AUC color thresholds:
// green > 0.58, amber 0.52–0.58, red < 0.52 / N/A.
function ModelPanel({ ml }) {
  const auc = ml.val_auc;
  const aucTone = auc == null ? "text-loss" : auc > 0.58 ? "text-gain"
    : auc >= 0.52 ? "text-watch" : "text-loss";
  const feats = Object.entries(ml.top_features || {});
  const maxImp = Math.max(...feats.map(([, v]) => v), 0.001);
  const ms = ml.method_stats || {};

  return (
    <Card title="Model performance"
          right={
            <span className={`text-[11px] px-2 py-0.5 rounded border ${
              ml.scoring_method_current === "ml"
                ? "border-info/50 text-info" : "border-line text-faint"}`}>
              scoring: {ml.scoring_method_current === "ml" ? "ML (XGBoost)" : "Rules"}
            </span>
          }>
      {!ml.enabled ? (
        <p className="text-sm text-mute">ML scoring disabled in config — running on the rule-based scorer.</p>
      ) : !ml.available ? (
        <p className="text-sm text-mute">
          No trained model yet — the rule-based scorer runs until 30 closed trades
          accumulate, then the first XGBoost model trains automatically.
          {ml.note && <span className="text-faint"> ({ml.note})</span>}
        </p>
      ) : (
        <div className="grid lg:grid-cols-3 gap-4">
          <div className="grid grid-cols-2 gap-3 content-start">
            <Mini label="Validation AUC" value={auc != null ? auc.toFixed(3) : "N/A"} tone={aucTone} />
            <Mini label="Training samples" value={ml.n_samples ?? "—"} />
            <Mini label="Last trained" value={ml.trained_at ? ml.trained_at.slice(0, 10) : "—"} />
            <Mini label="Freshness" value={ml.stale ? "stale" : "fresh"}
                  tone={ml.stale ? "text-watch" : "text-gain"} />
          </div>
          <div>
            <div className="lbl mb-2">Top feature importances</div>
            {feats.length === 0 ? <p className="text-xs text-faint">—</p> : feats.map(([name, v]) => (
              <div key={name} className="flex items-center gap-2 py-0.5">
                <span className="text-xs text-mute w-32 truncate">{name.replaceAll("_", " ")}</span>
                <div className="flex-1 h-1.5 bg-well rounded overflow-hidden">
                  <div className="h-full bg-info rounded" style={{ width: `${(v / maxImp) * 100}%` }} />
                </div>
                <span className="num text-[11px] text-faint w-10 text-right">{(v * 100).toFixed(0)}%</span>
              </div>
            ))}
          </div>
          <div>
            <div className="lbl mb-2">ML vs rules (closed trades)</div>
            <table className="w-full tbl">
              <thead><tr><th>Method</th><th>Trades</th><th>Win rate</th><th>Avg P&L</th></tr></thead>
              <tbody>
                {["ml", "rules"].map((m) => (
                  <tr key={m}>
                    <td className={m === "ml" ? "text-info" : ""}>{m === "ml" ? "ML" : "Rules"}</td>
                    <td className="num">{ms[m]?.n ?? 0}</td>
                    <td className="num">{fmtPct(ms[m]?.wr)}</td>
                    <td className={`num ${pnlCls(ms[m]?.avg_pnl)}`}>{fmt$(ms[m]?.avg_pnl)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="text-[11px] text-faint mt-2">
              The model only replaces signal-strength scoring; direction, sizing, and
              all risk controls stay deterministic. AUC ≥ 0.52 required to deploy.
            </p>
          </div>
        </div>
      )}
    </Card>
  );
}
