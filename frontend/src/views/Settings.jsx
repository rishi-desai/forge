import React, { useEffect, useState } from "react";
import { api, fmt$ } from "../api";
import { Card } from "../components/ui";

const CASH_PRESETS = [500, 1000, 5000, 10000, 25000, 100000];

const PROFILE_COPY = {
  conservative: ["Capital preservation first", "≤3% per trade · spreads & CSPs only · no 0DTE · halts at −3% day"],
  moderate: ["Balanced growth (default)", "≤8% per trade · full defined-risk playbook · ≤10% in 0DTE · halts at −5% day"],
  aggressive: ["Growth-seeking", "≤20% per trade · momentum & 0DTE up to 25% · halts at −8% day"],
  max_aggression: ["Maximum risk appetite", "≤40% per trade · 0DTE up to 50% · halts at −15% day"],
};

const TIERS = ["STRONG", "MAX", "ALL_IN"];

export default function Settings({ push }) {
  const [s, setS] = useState(null);
  const [cash, setCash] = useState(10000);
  const [customCash, setCustomCash] = useState("");
  const [profile, setProfile] = useState("moderate");
  const [ov, setOv] = useState({});
  const [adv, setAdv] = useState(false);
  const [custom, setCustom] = useState({});
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.settings().then((d) => {
      setS(d);
      setCash(d.config.account.starting_cash);
      setProfile(d.config.risk.profile);
      setOv(d.config.conviction_overrides || {});
      setCustom(d.config.risk.custom_overrides || {});
    });
  }, []);
  if (!s) return <div className="text-mute text-sm">Loading…</div>;

  const save = async () => {
    setSaving(true);
    const res = await api.saveSettings({
      starting_cash: Number(customCash) || cash,
      profile,
      conviction_overrides: ov,
      custom_overrides: adv ? custom : undefined,
    });
    setSaving(false);
    push?.(res.ok ? "Settings saved" : "Demo mode — backend offline",
      res.ok ? "New sizing applies to future trades only; existing positions are untouched."
             : "Start the backend to persist settings.", res.ok ? "gain" : "watch");
  };

  const effective = Number(customCash) || cash;
  const p = s.profiles[profile] || {};
  const example = Math.min(effective * (p.max_position_pct || 0.08) * (0.40 + 0.60 * 0.8),
                           p.max_position_abs_cap || 5000, 10000);

  return (
    <div className="space-y-4 max-w-3xl">
      <Card title="Starting cash (paper)">
        <div className="flex flex-wrap gap-2">
          {CASH_PRESETS.map((v) => (
            <button key={v} onClick={() => { setCash(v); setCustomCash(""); }}
              className={`px-3 py-1.5 rounded border text-sm num ${
                cash === v && !customCash ? "border-info text-info bg-info/10" : "border-line text-mute hover:text-text"}`}>
              {fmt$(v)}
            </button>
          ))}
          <input value={customCash} onChange={(e) => setCustomCash(e.target.value.replace(/[^\d]/g, ""))}
            placeholder="Custom" className="w-28 bg-well border border-line rounded px-3 py-1.5 text-sm num
            focus:outline-none focus-visible:ring-1 ring-info" />
        </div>
        <p className="text-[11px] text-faint mt-2">
          Reset your Alpaca paper account to match, or the ledger reconciles down to the broker's number.
        </p>
      </Card>

      <Card title="Risk profile">
        <div className="grid sm:grid-cols-2 gap-2">
          {Object.keys(PROFILE_COPY).map((k) => (
            <label key={k} className={`border rounded-md p-3 cursor-pointer block ${
              profile === k ? "border-info bg-info/5" : "border-line hover:border-faint"}`}>
              <div className="flex items-center gap-2">
                <input type="radio" name="profile" checked={profile === k}
                       onChange={() => setProfile(k)} className="accent-[#6CA8FF]" />
                <span className="text-sm font-medium">{k.replaceAll("_", " ")}</span>
              </div>
              <div className="text-xs text-mute mt-1">{PROFILE_COPY[k][0]}</div>
              <div className="text-[11px] text-faint mt-0.5">{PROFILE_COPY[k][1]}</div>
            </label>
          ))}
        </div>
        {profile === "max_aggression" && (
          <div className="mt-3 border border-loss/40 bg-loss/5 rounded px-3 py-2 text-xs text-loss">
            Max aggression risks rapid drawdowns — up to 40% of the account on one idea and
            half the book in 0DTE. Even on paper, results won't resemble a sustainable strategy.
            The −15% daily halt and $10,000 universal cap still apply and cannot be disabled.
          </div>
        )}
        <p className="text-xs text-mute mt-3">
          Example: with {fmt$(effective)} and a 0.80-strength spread signal, this profile sizes ≈{" "}
          <span className="num text-text">{fmt$(example)}</span>.
        </p>
      </Card>

      <Card title="High-conviction overrides" right={<span className="text-watch text-xs">⚡</span>}>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={!!ov.enabled}
                 onChange={(e) => setOv({ ...ov, enabled: e.target.checked })} className="accent-[#F5A623]" />
          Let the model exceed normal position sizing when its 7-point conviction checklist is met
        </label>
        {ov.enabled && (
          <div className="mt-3 space-y-3 text-sm">
            <div className="flex items-center gap-3">
              <span className="text-mute w-44">Max allowed tier</span>
              <select value={ov.max_allowed_tier || "MAX"}
                      onChange={(e) => setOv({ ...ov, max_allowed_tier: e.target.value })}
                      className="bg-well border border-line rounded px-2 py-1.5">
                {TIERS.map((t) => <option key={t}>{t}</option>)}
              </select>
              <span className="text-[11px] text-faint">
                STRONG 1.5× · MAX 2.0× · ALL-IN up to 50% of cash
              </span>
            </div>
            <div className="flex items-center gap-3">
              <span className="text-mute w-44">Override trades / day</span>
              <input type="number" min={0} max={10} value={ov.max_override_trades_per_day ?? 2}
                     onChange={(e) => setOv({ ...ov, max_override_trades_per_day: +e.target.value })}
                     className="w-20 bg-well border border-line rounded px-2 py-1.5 num" />
              <span className="text-mute">/ week</span>
              <input type="number" min={0} max={30} value={ov.max_override_trades_per_week ?? 5}
                     onChange={(e) => setOv({ ...ov, max_override_trades_per_week: +e.target.value })}
                     className="w-20 bg-well border border-line rounded px-2 py-1.5 num" />
            </div>
            <label className="flex items-center gap-2 text-mute">
              <input type="checkbox" checked={ov.all_in_requires_approval !== false}
                     onChange={(e) => setOv({ ...ov, all_in_requires_approval: e.target.checked })}
                     className="accent-[#F5A623]" />
              ALL-IN always asks me first (5-minute approval window, then normal size)
            </label>
            <p className="text-[11px] text-faint">
              Every override is logged with its criteria checklist; the Trade Log's Overrides tab
              tracks whether override trades actually outperform normal sizing.
            </p>
          </div>
        )}
      </Card>

      <Card title="Advanced: custom profile overrides">
        <label className="flex items-center gap-2 text-sm text-mute">
          <input type="checkbox" checked={adv} onChange={(e) => setAdv(e.target.checked)}
                 className="accent-[#6CA8FF]" />
          Override individual profile parameters (validated against universal hard caps)
        </label>
        {adv && (
          <div className="grid sm:grid-cols-2 gap-3 mt-3 text-sm">
            {[["max_position_pct", "Max position %", 0.01, 0.5, 0.01],
              ["max_position_abs_cap", "Abs cap per trade ($)", 100, 10000, 100],
              ["max_0dte_pct", "Max 0DTE %", 0, 0.5, 0.01],
              ["daily_loss_halt_pct", "Daily loss halt %", 0.01, 0.15, 0.01]].map(([k, label, min, max, step]) => (
              <label key={k} className="block">
                <span className="lbl">{label}</span>
                <input type="number" min={min} max={max} step={step}
                       value={custom[k] ?? ""} placeholder="profile default"
                       onChange={(e) => setCustom({ ...custom, [k]: e.target.value === "" ? undefined : +e.target.value })}
                       className="mt-1 w-full bg-well border border-line rounded px-2 py-1.5 num" />
              </label>
            ))}
          </div>
        )}
      </Card>

      <div className="flex items-center gap-3">
        <button onClick={save} disabled={saving}
                className="bg-info text-ink font-medium rounded px-4 py-2 hover:opacity-90 disabled:opacity-50">
          {saving ? "Saving…" : "Recalculate & apply"}
        </button>
        <span className="text-xs text-faint">
          Applies to future trades only — open positions keep their original sizing.
          The $10,000 / $5,000-per-contract universal caps can never be raised.
        </span>
      </div>
    </div>
  );
}
