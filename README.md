# Autonomous AI Paper-Trading System

Fully autonomous options-first paper trading on an Alpaca **cash** account, with a
self-learning signal engine, a tiered high-conviction override system, and a live
dashboard. Total running cost: **$0/month** (free API tiers + SQLite + local/free hosting).

> Paper trading only. Nothing here is investment advice, and the encoded heuristics
> are study material, not proven edges. See **Safety notes** at the bottom.

---

## Layout

```
ai-trading-system/
├── config.json                # account / risk / overrides / watchlist / cadence
├── requirements.txt
├── .env.example
├── backend/
│   ├── app.py                 # FastAPI + WebSocket; RUN_BOT=1 starts the loop
│   ├── orchestrator.py        # market clock, scan→signal→select→size→execute, exits
│   ├── risk.py                # profiles, sizing formula, settled-cash ledger, guards
│   ├── overrides.py           # 7-criteria conviction eval, STRONG/MAX/ALL-IN tiers
│   ├── signals.py             # indicators, rule engine, regime, composite score
│   ├── strategies.py          # playbook, strike/expiry construction, OCC symbols
│   ├── options_math.py        # Black-Scholes, greeks, IV solver, IV rank
│   ├── data_clients.py        # Finnhub REST+WS, FRED, F&G, sentiment, flow stubs
│   ├── alpaca_exec.py         # paper order execution incl. multi-leg (MLEG)
│   ├── learning.py            # post-trade analysis, weight updates, weekly report
│   ├── rationale.py           # plain-English trade rationale (+optional LLM polish)
│   ├── db.py                  # SQLite storage layer (Supabase swap point)
│   └── knowledge/             # distilled frameworks + arXiv paper ingester
├── tests/test_core.py         # 51 offline checks incl. every spec worked example
└── frontend/                  # React + Vite + Tailwind dashboard (7 views)
```

## Setup

**1. Keys (all free):**
- Alpaca paper account → https://alpaca.markets (use the *paper* keys). In the paper
  account settings, request **options level 3** (needed for spreads/condors) and set
  starting cash to whatever you choose in the dashboard.
- Finnhub → https://finnhub.io (free: 60 req/min, 50-symbol websocket)
- FRED → https://fred.stlouisfed.org/docs/api/api_key.html

**2. Backend:**
```bash
cd ai-trading-system
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in keys
cd backend
RUN_BOT=1 uvicorn app:app --port 8000
```
`RUN_BOT=0` serves the API/dashboard without trading (useful for review).

**3. Dashboard:**
```bash
cd frontend
npm install
npm run dev                     # http://localhost:5173
```
With the backend offline the dashboard runs on built-in demo data (banner shows
"demo data") so you can explore every view first.

**4. Optional:**
```bash
python backend/knowledge/ingest_papers.py    # cache recent arXiv q-fin abstracts
pip install transformers torch               # real FinBERT sentiment (else keyword fallback)
```

## How it trades (one pass)

1. **Pre-market (7:00–9:30 ET):** foreign ETF proxies + FX → overnight bias; FRED macro refresh.
2. **RTH, every 5 min:** per watchlist symbol — candles → indicators → rule engine
   fires signals → composite score (category weights × *learned* per-signal weights).
   Composite < 0.30 → no trade.
3. **Strategy selection:** 0DTE rules first (SPY/QQQ), earnings gate, then IV-rank
   routing (>50 sell premium, <30 buy premium, else spreads). Long options that the
   sized dollars can't afford downgrade automatically to debit spreads.
4. **Sizing:** `cash × profile% × (0.40 + 0.60 × strength) × strategy scalar`, capped by
   the profile's absolute cap and the $10,000 universal cap; ≥95% cash buffer respected.
5. **Conviction overrides:** strength ≥ 0.90 triggers the 7-criteria checklist —
   STRONG (1.5×, ≥5/7), MAX (2.0×, ≥6/7), ALL-IN (50% of cash, 7/7, **always** asks you
   via dashboard modal; 5-min timeout reverts to normal size). All capped at $10k.
6. **Cash-account validation:** settled-cash ledger (T+1), full collateral reserved for
   condors/CSPs, deployment ≤70%, ≤8 positions, ≤3 per sector, daily-loss halt,
   Wednesday weekly-loss size-halving.
7. **Management (every 60s):** profile stop-losses, 50%-of-credit profit target,
   8% equity stop, **0DTE force-closed at 3:00pm ET**, 15-min minimum option hold.
8. **Learning:** every closed trade updates per-signal weights (clamped 0.25–2.0×) and logs
   a lesson; Friday 4:30pm ET writes the weekly report (win rate, Sharpe, max DD,
   best/worst strategy with root cause, weight recommendations, vs SPY).

## ML adaptive signal engine

`signals.score_setup()` routes the **magnitude/confidence** component of scoring
through an XGBoost classifier when a trained, fresh (<14 days) model exists;
direction, sizing, and all risk controls stay deterministic. The model has no
authority over what trades, how much, or when.

- **Bootstrap:** every trade (rules-scored included) persists its 22-feature
  snapshot, so after **30 closed trades** the first model trains automatically.
- **Retraining:** Sundays 00:00 UTC, every 20 new closed trades, or
  `python backend/retrain_worker.py --force` / `POST /api/ml/retrain`. Rolling
  90-day window, chronological 80/20 split (never random), fixed hyperparameters
  (no tuning by design — the dataset is too small for it to be meaningful).
- **Deploy gate:** validation AUC ≥ 0.52 or the previous model stays; deploys are
  atomic (temp file + rename), safe alongside the live loop.
- **Fallbacks:** xgboost missing, model cold/stale/corrupt, `ml_engine.enabled:false`,
  or any scoring error → rule-based `composite_score()`. The bot can never crash
  from the ML layer.
- **Dashboard:** Learning view → *Model performance* panel (scoring method, AUC
  color-gated at 0.58/0.52, samples, top-5 importances, ML-vs-rules P&L).
- Each trade records `scoring_method`, so the ML-vs-rules comparison is measured
  on realized P&L, not backtests.

## Regime model

`regime_model.py` (Layer 3 of the target stack) classifies the market regime
with a GaussianMixture fit weekly on ~2 years of SPY daily features (20d
return, realized vol, 50d trend, ADX, drawdown). Clusters map to
trending_bull / trending_bear / ranging / high_vol by their statistics; the
directional label feeds the ML `regime` feature, while consumers keep the
legacy vocabulary. VIX > 35 is always `crisis` — the circuit breaker outranks
the model — and any failure (disabled, thin history, corrupt file) falls back
to the original VIX/ADX rule. Swap `GaussianMixture` for `hmmlearn` in
`fit()` if you want true temporal transitions later.

## Spec deviations (deliberate, flagged up front)

1. **$10k universal cap wins.** The spec's ALL-IN example ($8k STRONG → $12k) contradicts
   its own universal $10,000 limit; the limit binds everything, overrides included.
   Tests assert $8k STRONG → **$10k**.
2. **Override caps** follow §5.6.1 (profile abs cap × tier multiplier), then the $10k ceiling.
3. **Cash account ≠ "day trade freely."** Same-day proceeds aren't settled buying power;
   the ledger enforces T+1 to avoid good-faith violations. (PDT itself was retired by
   FINRA effective June 4 2026, but settlement rules still bind cash accounts.)
4. **No naked legs.** Pre-earnings short strangles → defined-risk iron condors; jade
   lizard dropped. PMCC kept (requires options level 3).
5. **SPX isn't on Alpaca** → 0DTE uses SPY/QQQ.
6. **No book piracy.** `knowledge/frameworks.json` encodes the reading list's frameworks
   as original-wording heuristics; paper ingestion is arXiv-only (open access).
7. **Scrape-based feeds (Barchart/Finviz/etc.)** are stub adapters with ToS notes; the
   running system uses only official APIs. Options flow is therefore demo data until
   you wire a permitted source into `OptionsFlowAdapter`.
8. **SQLite default**, single swap point in `db.py` for Supabase (below).
9. **"AI rationale" is deterministic templates** built from the actual fired signals —
   $0 and never invents reasons. Optional Anthropic polish is off by default.
10. **IV is proxied** by 20-day realized vol until you enable Alpaca's indicative options
    feed (`OptionDataClient` in alpaca-py) for true chain IV/greeks — marked in code.

## Supabase swap (optional)

`db.py` is the single seam: reimplement its ~15 functions against `supabase-py`
(tables map 1:1 — `trades`, `trade_signals`, `signal_weights`, `lessons`,
`overrides_log`, `rejected_overrides`, `equity_curve`, `weekly_reports`,
`live_signals`, `events`). Free tier is plenty at this volume. SQLite is genuinely
fine for a single-bot deployment, so this is only worth it if you want the dashboard
hosted separately from the bot.

## Deployment

Recommended: **run locally** (the bot is a long-lived loop; free hosting tiers sleep).
If hosting anyway: backend on Render free tier (sleeps after 15 min idle — fine for
the dashboard API, *not* for the bot; keep the bot loop on a machine that stays awake,
e.g. a homelab box), frontend on Vercel (`FRONTEND_ORIGIN` in `.env`, `VITE_API_URL`
in `frontend/.env`).

## Safety notes

- Paper only. Run **≥3 months** of paper results — including a red week — before even
  discussing real money, and treat the strategy heuristics as hypotheses the learning
  loop is testing, not edges.
- Free data tiers are delayed/indicative in places; fills and marks will differ from
  a live environment. The `_estimate_mark` placeholder holds positions at cost until
  real option marks are wired in, so unrealized P&L is conservative.
- The universal caps ($10k/position, $5k/contract, 95% cash buffer) are hard-coded on
  purpose and not exposed in Settings.
