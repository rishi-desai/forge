import React, { useEffect, useState } from "react";
import { api } from "../api";
import { Card, DirBadge } from "../components/ui";
import { MOCK } from "../mock";

const ETF_NAMES = {
  EWJ: "Japan (Nikkei proxy)", EWG: "Germany (DAX proxy)", EWU: "UK (FTSE proxy)",
  FXI: "China large-cap", INDA: "India", EWZ: "Brazil",
};
const FX_NOTES = {
  USDJPY: "↑ = risk-on / carry intact", EURUSD: "↑ = softer dollar",
  DXY: "↑ = dollar strength (risk-off lean)",
};

const Pct = ({ v }) => (
  <span className={`num font-mono font-semibold ${v > 0 ? "text-gain" : v < 0 ? "text-loss" : "text-mute"}`}>
    {v > 0 ? "+" : ""}{v?.toFixed(2)}%
  </span>
);

export default function Foreign() {
  const [d, setD] = useState(null);
  useEffect(() => {
    api.foreign().then((r) => {
      // Backend serves summary+bias; the detailed tables come from mock until the
      // pre-market refresh has run (it populates moves via Finnhub ETF quotes).
      setD({ ...MOCK.foreign, ...r, moves: r.moves || MOCK.foreign.moves,
             fx: r.fx || MOCK.foreign.fx, futures: r.futures || MOCK.foreign.futures });
    });
  }, []);
  if (!d) return <div className="text-mute text-sm">Loading…</div>;

  return (
    <div className="space-y-4">
      <Card title="Pre-market read">
        <div className="flex items-center gap-3">
          <DirBadge d={d.bias} />
          <p className="text-sm text-mute">{d.summary || "Populates during the 7:00–9:30am ET pre-market scan."}</p>
        </div>
      </Card>

      <div className="grid lg:grid-cols-3 gap-4">
        <Card title="International ETF proxies (overnight sessions)" className="lg:col-span-2">
          <table className="w-full tbl">
            <thead><tr><th>ETF</th><th>Market</th><th>Move</th><th>Read</th></tr></thead>
            <tbody>
              {Object.entries(d.moves).map(([sym, v]) => (
                <tr key={sym}>
                  <td className="font-mono">{sym}</td>
                  <td className="text-mute">{ETF_NAMES[sym] || sym}</td>
                  <td><Pct v={v} /></td>
                  <td className="text-xs text-faint">
                    {Math.abs(v) >= 1.5 ? (v > 0 ? "strong overnight bid" : "overnight stress — gap risk")
                      : Math.abs(v) >= 0.5 ? "modest drift" : "quiet"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-[11px] text-faint mt-2">
            Nikkei −2% historically leans SPY lower at the open; DAX strength supports
            risk appetite. These feed the pre-market overlay rules, never trades by themselves.
          </p>
        </Card>

        <div className="space-y-4">
          <Card title="FX">
            {Object.entries(d.fx).map(([pair, v]) => (
              <div key={pair} className="flex items-center justify-between py-1.5 border-b border-line/50 last:border-0">
                <div>
                  <span className="font-mono text-sm">{pair}</span>
                  <div className="text-[11px] text-faint">{FX_NOTES[pair]}</div>
                </div>
                <Pct v={v} />
              </div>
            ))}
          </Card>
          <Card title="US futures">
            {Object.entries(d.futures).map(([sym, v]) => (
              <div key={sym} className="flex items-center justify-between py-1.5">
                <span className="font-mono text-sm">{sym === "ES" ? "ES (S&P 500)" : "NQ (Nasdaq 100)"}</span>
                <Pct v={v} />
              </div>
            ))}
            <p className="text-[11px] text-faint mt-2">
              Futures &gt; ±0.5% pre-market arms the gap-fade / gap-go open rules.
            </p>
          </Card>
        </div>
      </div>
    </div>
  );
}
