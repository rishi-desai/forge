import React, { useEffect, useState } from "react";
import { api, fmt$ } from "../api";
import { Card, DirBadge } from "../components/ui";

// Unusual-flow demo rows shown until an options-flow adapter is configured.
// The backend's OptionsFlowAdapter is a stub by design (no free official feed);
// see README "Options flow data" for the supported ways to wire one in.
const DEMO_FLOW = [
  { ts: "10:42", symbol: "NVDA", kind: "sweep", side: "call", strike: 1280, expiry: "2026-06-26", premium: 2_100_000, vol_oi: 6.4, direction: "bullish" },
  { ts: "10:38", symbol: "AAPL", kind: "block", side: "call", strike: 235, expiry: "2026-07-17", premium: 870_000, vol_oi: 3.1, direction: "bullish" },
  { ts: "10:31", symbol: "TSLA", kind: "sweep", side: "put", strike: 310, expiry: "2026-06-19", premium: 640_000, vol_oi: 4.8, direction: "bearish" },
  { ts: "10:17", symbol: "SPY", kind: "block", side: "put", strike: 600, expiry: "2026-09-18", premium: 5_400_000, vol_oi: 1.2, direction: "hedge?" },
  { ts: "09:58", symbol: "AMD", kind: "sweep", side: "call", strike: 210, expiry: "2026-06-26", premium: 520_000, vol_oi: 5.5, direction: "bullish" },
];

const IV_LEADERS = [
  { symbol: "TSLA", iv_rank: 78 }, { symbol: "AMD", iv_rank: 64 },
  { symbol: "NVDA", iv_rank: 28 }, { symbol: "AAPL", iv_rank: 24 },
  { symbol: "SPY", iv_rank: 21 }, { symbol: "MSFT", iv_rank: 19 },
];

export default function Flow() {
  const [pcr] = useState(0.87); // CBOE total put/call; backend fetches best-effort
  const [demo, setDemo] = useState(true);
  useEffect(() => { api.portfolio().then(() => {}); }, []);

  return (
    <div className="space-y-4">
      {demo && (
        <p className="text-xs text-info border border-info/30 rounded px-3 py-2 bg-info/5">
          Showing sample flow. Live unusual-activity data needs a flow source —
          the backend's OptionsFlowAdapter stub documents the options (see README).
        </p>
      )}
      <div className="grid lg:grid-cols-3 gap-4">
        <Card title="Unusual options activity" className="lg:col-span-2 overflow-auto max-h-[60vh]">
          <table className="w-full tbl">
            <thead><tr>
              <th>Time</th><th>Ticker</th><th>Type</th><th>Contract</th>
              <th>Premium</th><th>Vol/OI</th><th>Read</th>
            </tr></thead>
            <tbody>
              {DEMO_FLOW.map((f, i) => (
                <tr key={i}>
                  <td className="num font-mono text-mute">{f.ts}</td>
                  <td className="font-mono">{f.symbol}</td>
                  <td className="text-mute">{f.kind}</td>
                  <td className="font-mono text-xs">
                    {f.expiry.slice(5)} {f.strike}{f.side === "call" ? "C" : "P"}
                  </td>
                  <td className={`num font-semibold ${f.premium >= 500_000 ? "text-watch" : ""}`}>
                    {fmt$(f.premium)}
                  </td>
                  <td className="num">{f.vol_oi.toFixed(1)}×</td>
                  <td>{f.direction === "hedge?" ? <span className="text-faint text-xs">hedge?</span> : <DirBadge d={f.direction} />}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-[11px] text-faint mt-2">
            Premium ≥ $500K in trade direction is conviction-override criterion #2 (amber).
          </p>
        </Card>

        <div className="space-y-4">
          <Card title="Put / Call ratio">
            <div className="num text-3xl font-semibold">{pcr.toFixed(2)}</div>
            <div className="h-2 bg-well rounded mt-3 relative overflow-hidden">
              <div className="absolute inset-y-0 left-[30%] right-[30%] bg-line/60" />
              <div className="absolute inset-y-0 w-1 bg-info rounded"
                   style={{ left: `${Math.min(Math.max((pcr - 0.4) / 1.0, 0), 1) * 100}%` }} />
            </div>
            <div className="flex justify-between text-[11px] text-faint mt-1">
              <span>0.4 greedy</span><span>0.7–1.0 normal</span><span>1.4 fearful</span>
            </div>
            <p className="text-[11px] text-faint mt-2">
              &gt;1.20 = extreme fear (contrarian bullish) · &lt;0.70 = complacency (caution).
            </p>
          </Card>

          <Card title="IV rank leaderboard">
            {IV_LEADERS.map((s) => (
              <div key={s.symbol} className="flex items-center gap-3 py-1">
                <span className="font-mono w-12 text-sm">{s.symbol}</span>
                <div className="flex-1 h-1.5 bg-well rounded overflow-hidden">
                  <div className={`h-full rounded ${s.iv_rank > 50 ? "bg-gain" : "bg-info"}`}
                       style={{ width: `${s.iv_rank}%` }} />
                </div>
                <span className="num text-xs text-mute w-8 text-right">{s.iv_rank}</span>
              </div>
            ))}
            <p className="text-[11px] text-faint mt-2">
              &gt;50: sell premium (condors, CSPs) · &lt;30: buy premium (long options, debit spreads).
            </p>
          </Card>
        </div>
      </div>
    </div>
  );
}
