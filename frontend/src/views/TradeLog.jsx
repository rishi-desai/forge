import React, { useEffect, useMemo, useState } from "react";
import { api, fmt$, fmtPct, pnlCls } from "../api";
import { Card, ConvictionMeter, TierBadge } from "../components/ui";

export default function TradeLog() {
  const [tab, setTab] = useState("all");
  const [trades, setTrades] = useState([]);
  const [ovr, setOvr] = useState(null);
  const [q, setQ] = useState("");
  const [filt, setFilt] = useState({ result: "all", strategy: "all" });
  const [open, setOpen] = useState(null);

  useEffect(() => {
    api.trades().then((d) => setTrades(d.trades || []));
    api.overrides().then(setOvr);
  }, []);

  const strategies = useMemo(() => [...new Set(trades.map((t) => t.strategy))], [trades]);
  const rows = useMemo(() => trades.filter((t) =>
    (q === "" || t.symbol.toLowerCase().includes(q.toLowerCase())) &&
    (filt.strategy === "all" || t.strategy === filt.strategy) &&
    (filt.result === "all" ||
      (filt.result === "win" && (t.pnl || 0) > 0) ||
      (filt.result === "loss" && t.status === "closed" && (t.pnl || 0) <= 0) ||
      (filt.result === "open" && t.status === "open"))
  ), [trades, q, filt]);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        {["all", "overrides"].map((k) => (
          <button key={k} onClick={() => setTab(k)}
            className={`text-sm px-3 py-1.5 rounded border ${
              tab === k ? "border-line bg-panel" : "border-transparent text-mute hover:text-text"}`}>
            {k === "all" ? "All trades" : "⚡ Overrides"}
          </button>
        ))}
        {tab === "all" && (
          <div className="ml-auto flex gap-2">
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search symbol"
              className="bg-well border border-line rounded px-3 py-1.5 text-sm w-40 focus:outline-none focus-visible:ring-1 ring-info" />
            <select value={filt.strategy} onChange={(e) => setFilt({ ...filt, strategy: e.target.value })}
              className="bg-well border border-line rounded px-2 py-1.5 text-sm">
              <option value="all">All strategies</option>
              {strategies.map((s) => <option key={s} value={s}>{s.replaceAll("_", " ")}</option>)}
            </select>
            <select value={filt.result} onChange={(e) => setFilt({ ...filt, result: e.target.value })}
              className="bg-well border border-line rounded px-2 py-1.5 text-sm">
              <option value="all">All results</option>
              <option value="win">Wins</option><option value="loss">Losses</option>
              <option value="open">Open</option>
            </select>
          </div>
        )}
      </div>

      {tab === "all" ? (
        <Card className="overflow-auto max-h-[72vh]">
          <table className="w-full tbl">
            <thead><tr>
              <th>Date</th><th>Symbol</th><th>Strategy</th><th>Size</th>
              <th>P&L</th><th>Status</th><th>Profile</th><th></th>
            </tr></thead>
            <tbody>
              {rows.map((t) => (
                <React.Fragment key={t.id}>
                  <tr className="cursor-pointer" onClick={() => setOpen(open === t.id ? null : t.id)}>
                    <td className="num font-mono text-mute whitespace-nowrap">{t.ts_open?.slice(0, 10)}</td>
                    <td className="font-mono">{t.symbol}</td>
                    <td><div className="flex items-center gap-2">
                      {t.strategy?.replaceAll("_", " ")} <TierBadge tier={t.override_tier} />
                    </div></td>
                    <td className="num font-semibold">{fmt$(t.sized_dollars ?? t.cost)}</td>
                    <td className={`num font-semibold ${pnlCls(t.pnl)}`}>
                      {t.status === "open" ? "—" : fmt$(t.pnl)}</td>
                    <td>{t.status === "open"
                      ? <span className="text-info text-xs">open</span>
                      : (t.pnl || 0) > 0
                        ? <span className="text-gain text-xs">win</span>
                        : <span className="text-loss text-xs">loss</span>}</td>
                    <td className="text-mute text-xs">{t.risk_profile}</td>
                    <td className="text-faint">{open === t.id ? "▾" : "▸"}</td>
                  </tr>
                  {open === t.id && (
                    <tr><td colSpan={8} className="!bg-well">
                      <pre className="whitespace-pre-wrap text-xs text-mute font-mono p-3 leading-relaxed">
                        {t.rationale || "No rationale recorded."}
                      </pre>
                    </td></tr>
                  )}
                </React.Fragment>
              ))}
            </tbody>
          </table>
        </Card>
      ) : (
        <OverridesPanel ovr={ovr} />
      )}
    </div>
  );
}

function OverridesPanel({ ovr }) {
  if (!ovr) return <div className="text-mute text-sm">Loading…</div>;
  const { stats } = ovr;
  return (
    <div className="space-y-4">
      <div className="grid md:grid-cols-2 gap-4">
        <Card title="Override vs normal performance">
          <table className="w-full tbl">
            <thead><tr><th></th><th>Trades</th><th>Win rate</th><th>Avg P&L</th></tr></thead>
            <tbody>
              <tr><td>Normal sizing</td>
                <td className="num">{stats?.normal?.n ?? 0}</td>
                <td className="num">{fmtPct(stats?.normal?.wr)}</td>
                <td className={`num ${pnlCls(stats?.normal?.avg_pnl)}`}>{fmt$(stats?.normal?.avg_pnl)}</td></tr>
              <tr><td className="text-watch">⚡ Override sizing</td>
                <td className="num">{stats?.override?.n ?? 0}</td>
                <td className="num">{fmtPct(stats?.override?.wr)}</td>
                <td className={`num ${pnlCls(stats?.override?.avg_pnl)}`}>{fmt$(stats?.override?.avg_pnl)}</td></tr>
            </tbody>
          </table>
          <p className="text-[11px] text-faint mt-2">
            This comparison is the evidence for whether the model's high-conviction calls
            are actually better — adjust the max allowed tier in Settings accordingly.
          </p>
        </Card>
        <Card title="Blocked override attempts">
          {(ovr.rejected || []).length === 0
            ? <p className="text-sm text-mute">None yet.</p>
            : (ovr.rejected || []).map((r, i) => (
              <div key={i} className="text-xs py-1.5 border-b border-line/50 flex gap-2">
                <span className="font-mono text-mute">{r.ts?.slice(5, 16)}</span>
                <span className="font-mono">{r.symbol}</span>
                <TierBadge tier={r.tier} />
                <span className="text-faint">{r.reason}</span>
              </div>
            ))}
        </Card>
      </div>

      <Card title="Trades executed via override">
        {(ovr.override_trades || []).map((t) => (
          <div key={t.id} className="border border-line rounded-md p-3 mb-3">
            <div className="flex items-center gap-3 flex-wrap">
              <TierBadge tier={t.override_tier} />
              <span className="font-mono">{t.symbol}</span>
              <span className="text-sm text-mute">{t.strategy?.replaceAll("_", " ")}</span>
              <span className="num text-sm">
                {fmt$(t.override_normal_size)} <span className="text-faint">normal →</span>{" "}
                <span className="text-watch font-semibold">{fmt$(t.sized_dollars)}</span>
              </span>
              <span className={`num ml-auto font-semibold ${pnlCls(t.pnl)}`}>
                {t.status === "open" ? "open" : fmt$(t.pnl)}
              </span>
            </div>
            <pre className="whitespace-pre-wrap text-xs text-mute font-mono mt-2 leading-relaxed">
              {t.rationale}
            </pre>
          </div>
        ))}
      </Card>
    </div>
  );
}
