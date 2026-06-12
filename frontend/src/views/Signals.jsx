import React, { useEffect, useState } from "react";
import { api } from "../api";
import { Card, DirBadge, StrengthBar } from "../components/ui";

export default function Signals() {
  const [d, setD] = useState(null);
  useEffect(() => {
    const load = () => api.signals().then(setD);
    load();
    const id = setInterval(load, 30_000); // spec: auto-refresh every 30s
    return () => clearInterval(id);
  }, []);
  if (!d) return <div className="text-mute text-sm">Loading…</div>;

  return (
    <div className="space-y-4">
      <Card title="Active signals" right={<span className="text-[11px] text-faint">refreshes every 30s</span>}
            className="overflow-auto max-h-[70vh]">
        <table className="w-full tbl">
          <thead><tr>
            <th>Time</th><th>Ticker</th><th>Signal</th><th>Direction</th>
            <th>Strength</th><th>Suggested strategy</th><th>Detail</th>
          </tr></thead>
          <tbody>
            {d.signals.map((s, i) => (
              <tr key={i}>
                <td className="num font-mono text-mute whitespace-nowrap">
                  {new Date(s.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                </td>
                <td className="font-mono">{s.symbol}</td>
                <td>{s.name?.replaceAll("_", " ")}</td>
                <td><DirBadge d={s.direction} /></td>
                <td><div className="flex items-center gap-2">
                  <StrengthBar v={s.strength} />
                  <span className="num text-xs text-mute">{Math.round(s.strength * 100)}</span>
                </div></td>
                <td className="text-mute">{s.suggested_strategy?.replaceAll("_", " ") || "—"}</td>
                <td className="text-mute max-w-xs truncate" title={s.detail}>{s.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      <p className="text-xs text-faint">
        Market regime: <span className="text-text">{d.regime?.replaceAll("_", " ") ?? "—"}</span>.
        Signals below 0.30 composite strength never trade; 0.90+ enters conviction-override evaluation.
      </p>
    </div>
  );
}
