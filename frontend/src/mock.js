// mock.js — realistic demo data so every view renders before keys are wired in.
const now = Date.now();
const day = 86400e3;
const curve = Array.from({ length: 60 }, (_, i) => {
  const drift = i * 14 + Math.sin(i / 4) * 120 + (i > 40 ? (i - 40) * 22 : 0);
  return { ts: new Date(now - (60 - i) * day).toISOString(),
           equity: 10000 + Math.round(drift), cash: 6200, deployed: 3800 };
});

const rationale = `Bull Call Spread on NVDA (bullish, 14 DTE, IV rank 28).

Signals that fired:
  • [options] institutional_call_flow (bullish, 0.85): $2.1M unusual call premium
  • [technical] oversold_in_uptrend (bullish, 0.75): RSI 33 < 35, price above 200d MA
  • [sentiment] news_sentiment (bullish, 0.42): FinBERT news score +0.70

Composite signal strength: 0.96 → signal scalar 0.98.
Risk profile 'moderate': max 8% per trade, abs cap $5,000.
Sized at $1,880.00. Market regime: trending.
Defined risk: max loss $1,880, max profit $6,240.
Catalyst: earnings in 18d.`;

const ovRationale = `⚡⚡ HIGH CONVICTION — MAX OVERRIDE (2.0×)

Trade: NVDA bull call spread, 14 DTE
Normal size: $1,880 → Override size: $3,760

Why this trade was flagged as exceptional:
  ✅ Full signal alignment: technical, options flow, macro, sentiment agree
  ✅ Institutional flow: >$500K unusual premium in trade direction
  ❌ Risk/reward ≥ 3:1 with defined max loss
  ✅ Catalyst present
  ✅ IV rank favorable for this strategy type
  ✅ Benign market regime (VIX < 35, Fear & Greed > 15)
  ✅ Historical analogs ≥ 70% win rate on ≥ 20 samples

Criteria met: 6/7 → qualifies for MAX tier
Override approved: auto-execute`;

const trades = [
  { id: 7, ts_open: new Date(now - 2 * 3600e3).toISOString(), symbol: "NVDA",
    strategy: "bull_call_spread", strategy_type: "spread", direction: "bullish",
    contracts: 2, cost: 3760, pnl: null, status: "open", signal_strength: 0.96,
    iv_rank: 28, market_regime: "trending", risk_profile: "moderate",
    sized_dollars: 3760, rationale: ovRationale, override_tier: "MAX",
    override_normal_size: 1880, dte: 14, expiry: "2026-06-26", max_profit: 6240, max_loss: 3760 },
  { id: 6, ts_open: new Date(now - day).toISOString(), ts_close: new Date(now - 3600e3).toISOString(),
    symbol: "SPY", strategy: "0dte_iron_condor", strategy_type: "0dte", direction: "neutral",
    contracts: 1, cost: 500, pnl: 118, status: "closed", signal_strength: 0.66,
    iv_rank: 64, market_regime: "range_bound", risk_profile: "moderate",
    sized_dollars: 500, rationale: "0DTE Iron Condor on SPY — flat open, VIX 15.8.\nClosed at 50% of max credit.",
    override_tier: null, dte: 0, expiry: "2026-06-09", max_profit: 240, max_loss: 260 },
  { id: 5, ts_open: new Date(now - 3 * day).toISOString(), ts_close: new Date(now - 2 * day).toISOString(),
    symbol: "MSFT", strategy: "cash_secured_put", strategy_type: "spread", direction: "bullish",
    contracts: 1, cost: 39000 / 10, pnl: 142, status: "closed", signal_strength: 0.71,
    iv_rank: 58, market_regime: "trending", risk_profile: "moderate",
    sized_dollars: 800, rationale: rationale.replace("NVDA", "MSFT"),
    override_tier: null, dte: 30, expiry: "2026-07-10", max_profit: 310, max_loss: 3580 },
  { id: 4, ts_open: new Date(now - 5 * day).toISOString(), ts_close: new Date(now - 4 * day).toISOString(),
    symbol: "QQQ", strategy: "bear_put_spread", strategy_type: "spread", direction: "bearish",
    contracts: 1, cost: 420, pnl: -208, status: "closed", signal_strength: 0.62,
    iv_rank: 31, market_regime: "high_volatility", risk_profile: "moderate",
    sized_dollars: 420, rationale: rationale.replace("Bull Call", "Bear Put").replace("NVDA", "QQQ"),
    override_tier: null, dte: 21, expiry: "2026-06-26", max_profit: 580, max_loss: 420 },
];

export const MOCK = {
  portfolio: {
    cash: 6240, deployed: 3760, equity: curve.at(-1).equity,
    total_pnl: 487, today_pnl: 118, win_rate: 0.667,
    open_positions: trades.filter(t => t.status === "open"),
    equity_curve: curve, regime: "trending", regime_detail: "trending_bull", regime_confidence: 0.84,
    regime_source: "model", vix: 15.8, fear_greed: 61, profile: "moderate",
  },
  signals: { regime: "trending", signals: [
    { ts: new Date(now - 6e5).toISOString(), symbol: "NVDA", name: "institutional_call_flow", direction: "bullish", strength: 0.85, suggested_strategy: "bull_call_spread", detail: "$2.1M unusual call premium" },
    { ts: new Date(now - 9e5).toISOString(), symbol: "AAPL", name: "oversold_in_uptrend", direction: "bullish", strength: 0.75, suggested_strategy: "long_call", detail: "RSI 33 < 35, price above 200d MA, IV rank 24" },
    { ts: new Date(now - 12e5).toISOString(), symbol: "SPY", name: "0dte_flat_open_condor", direction: "neutral", strength: 0.65, suggested_strategy: "0dte_iron_condor", detail: "Flat open (+0.12%), VIX 15.8 < 18" },
    { ts: new Date(now - 15e5).toISOString(), symbol: "XLE", name: "sector_rotation", direction: "bullish", strength: 0.60, suggested_strategy: "equity_momentum", detail: "Sector outperforming SPY 6 straight days" },
    { ts: new Date(now - 18e5).toISOString(), symbol: "TSLA", name: "overbought_failed_reclaim", direction: "bearish", strength: 0.75, suggested_strategy: "long_put", detail: "RSI 74 > 70 under 20d MA, IV rank 27" },
  ]},
  trades: { trades },
  overrides: {
    override_trades: trades.filter(t => t.override_tier),
    stats: { normal: { n: 3, avg_pnl: 17.3, wr: 0.667 }, override: { n: 1, avg_pnl: null, wr: null } },
    pending: [{
      approval_id: "demo123", tier: "ALL_IN", symbol: "AMD", strategy: "bull_call_spread",
      normal_size: 740, expires_at: new Date(now + 4 * 60e3).toISOString(),
      criteria: { full_signal_alignment: true, institutional_options_flow: true,
        favorable_risk_reward: true, catalyst_present: true, favorable_iv: true,
        benign_market_regime: true, strong_historical_analog: true },
    }],
    rejected: [{ ts: new Date(now - 2 * day).toISOString(), symbol: "META", tier: "STRONG",
      criteria_met: 5, reason: "global daily override limit reached", signal_strength: 0.91 }],
  },
  foreign: {
    bias: "bullish",
    summary: "Pre-market bias BULLISH: foreign ETF proxies averaged +0.4%, 2 overlay rule(s) active.",
    moves: { EWJ: 0.8, EWG: 1.2, EWU: 0.3, FXI: -0.5, INDA: 0.6, EWZ: 1.1 },
    fx: { USDJPY: 0.2, EURUSD: 0.1, DXY: -0.2 },
    futures: { ES: 0.31, NQ: 0.44 },
  },
  learning: {
    ml: {
      enabled: true, available: true, stale: false, scoring_method_current: "ml",
      trained_at: new Date(now - 2 * day).toISOString(), n_samples: 47, val_auc: 0.61,
      top_features: { signal_consensus: 0.21, iv_rank: 0.17, rsi_14: 0.14,
                      vix_level: 0.11, adx_14: 0.09 },
      method_stats: { ml: { n: 12, avg_pnl: 38.4, wr: 0.667 },
                      rules: { n: 35, avg_pnl: 11.2, wr: 0.571 } },
      runs: [{ trained_at: new Date(now - 2 * day).toISOString(), n_samples: 47,
               val_auc: 0.61, val_accuracy: 0.64, deployed: 1 }],
    },
    weekly_reports: [{ week_ending: "2026-06-05", total_trades: 9, win_rate: 0.667,
      gross_pnl: 1240, gross_loss: -612, net_pnl: 628, sharpe: 1.8, max_drawdown: 0.031,
      best_strategy: { name: "0dte_iron_condor", n: 3, pnl: 402, wins: 3 },
      worst_strategy: { name: "bear_put_spread", n: 2, pnl: -310, wins: 0,
        root_cause: "2 losers, most in 'high_volatility' regime — review whether the entry rule fits that regime." },
      signals_overperforming: [{ name: "institutional_call_flow", weight: 1.31 }, { name: "0dte_flat_open_condor", weight: 1.18 }],
      signals_underperforming: [{ name: "extreme_put_call", weight: 0.74 }],
      recommended_adjustments: ["Increase reliance on 'institutional_call_flow'", "Reduce reliance on 'extreme_put_call'"],
      portfolio_week_return: 0.046, spy_week_return: 0.012, vs_spy: 0.034 }],
    signal_weights: [
      { name: "institutional_call_flow", weight: 1.31, samples: 6, correct: 5 },
      { name: "0dte_flat_open_condor", weight: 1.18, samples: 4, correct: 3 },
      { name: "oversold_in_uptrend", weight: 1.05, samples: 7, correct: 4 },
      { name: "high_iv_rangebound", weight: 0.98, samples: 5, correct: 3 },
      { name: "news_sentiment", weight: 0.89, samples: 8, correct: 4 },
      { name: "extreme_put_call", weight: 0.74, samples: 3, correct: 1 },
    ],
    lessons: [{ ts: new Date(now - day).toISOString(), trade_id: 4, strategy: "bear_put_spread",
      signal_accuracy: -0.41, what_worked: '["news_sentiment"]',
      what_failed: '["spy_50d_breakdown_cheap_vol"]', market_context: "high_volatility" }],
  },
  settings: {
    config: {
      account: { starting_cash: 10000, account_type: "cash" },
      risk: { profile: "moderate", custom_overrides: {} },
      conviction_overrides: { enabled: true, max_allowed_tier: "MAX",
        auto_execute_strong: true, auto_execute_max: true, all_in_requires_approval: true,
        max_override_trades_per_day: 2, max_override_trades_per_week: 5 },
    },
    profiles: {
      conservative: { max_position_pct: 0.03, max_position_abs_cap: 3000, allow_0dte: false },
      moderate: { max_position_pct: 0.08, max_position_abs_cap: 5000, allow_0dte: true },
      aggressive: { max_position_pct: 0.20, max_position_abs_cap: 8000, allow_0dte: true },
      max_aggression: { max_position_pct: 0.40, max_position_abs_cap: 5000, allow_0dte: true },
    },
    hard_caps: { absolute_max_single_position: 10000 },
  },
};
