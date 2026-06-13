"""
orchestrator.py — The bot loop (wires Layers 1-5 together).

Cadence (all America/New_York):
  07:00–09:30  pre-market: foreign overnight panel, macro refresh, watchlist prep
  09:30–16:00  every `scan_interval_minutes`: scan → signal → select → size →
               override eval → risk validation → execute; every 60s: manage positions
  15:00        force-close all 0DTE positions
  Fri 16:30    weekly report
Daily halt / weekly size-halving guards apply throughout. Minimum 15-minute hold
for options (no HFT, spec constraint #7).
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
import traceback
from zoneinfo import ZoneInfo

import pandas as pd

import db
import learning
from alpaca_exec import PaperBroker
from data_clients import (FinnhubClient, FREDClient, OptionsFlowAdapter,
                          SentimentScorer, fear_greed_index)
from options_math import (bs_price, implied_vol, iv_rank as compute_iv_rank,
                          strike_increment_for)
from overrides import OverrideManager
from rationale import (llm_polish, normal_rationale, overnight_summary_sentence,
                       override_rationale)
from regime_model import RegimeModel
from risk import (PortfolioGuards, RiskRejection, SettledCashLedger,
                  UNIVERSAL_HARD_CAPS, compute_position_size, get_profile)
from signals import (MarketContext, SymbolSnapshot, classify_regime,
                     compute_indicator_frame, score_setup,
                     foreign_overnight_signals, macro_signals,
                     technical_and_options_rules)
from strategies import contracts_for_dollars, select_strategy

ET = ZoneInfo("America/New_York")


class Orchestrator:
    def __init__(self, config: dict, broker: PaperBroker | None = None,
                 on_event=None):
        self.cfg = config
        self.profile = get_profile(config["risk"]["profile"],
                                   config["risk"].get("custom_overrides"))
        self.broker = broker or PaperBroker()
        self.finnhub = FinnhubClient()
        self.fred = FREDClient()
        self.flow = OptionsFlowAdapter()
        self.sentiment = SentimentScorer()
        self.guards = PortfolioGuards(self.profile)
        self.overrides = OverrideManager(config["conviction_overrides"])
        self.regime_model = RegimeModel()
        snap = self.broker.account_snapshot()
        self.ledger = SettledCashLedger(settled=snap["cash"]
                                        or config["account"]["starting_cash"])
        self.ctx = MarketContext()
        self.on_event = on_event or (lambda kind, payload: None)
        self._open_meta: dict = {}  # trade_id → {opened_at, candidate}
        self._pending_ovr: dict = {}  # approval_id → execution context (cand, etc.)
        self._spy_ret63: float | None = None  # cached SPY 3m return for rel-strength
        self._last_scan = self._last_manage = 0.0
        # Serializes the execute/manage paths (bot loop thread) against the
        # approval path (API thread) so ledger + DB writes don't interleave.
        self._lock = threading.RLock()
        if self.profile.get("startup_warning"):
            db.log_event("warn", "profile", self.profile["startup_warning"])

    # ------------------------------------------------------------------ clock

    @staticmethod
    def now_et() -> dt.datetime:
        return dt.datetime.now(ET)

    def market_phase(self, now: dt.datetime | None = None) -> str:
        now = now or self.now_et()
        if now.weekday() >= 5:
            return "closed"
        t = now.time()
        if dt.time(4, 0) <= t < dt.time(9, 30):
            return "pre"
        if dt.time(9, 30) <= t < dt.time(16, 0):
            return "open"
        if dt.time(16, 0) <= t < dt.time(20, 0):
            return "after"
        return "closed"

    # ------------------------------------------------------------------ context refresh

    def refresh_macro(self):
        m = self.fred.macro_snapshot()
        self.ctx.vix = m.get("vix_close") or self.ctx.vix
        self.ctx.yield_curve_2s10s = (m.get("yield_curve_2s10s")
                                      if m.get("yield_curve_2s10s") is not None
                                      else self.ctx.yield_curve_2s10s)
        # DXY trend feeds the dxy_strength macro rule (previously stuck neutral).
        dxy_chg = self._fred_pct_change("DTWEXBGS")
        if dxy_chg is not None:
            self.ctx.dxy_trend = ("bullish" if dxy_chg > 0.1
                                  else "bearish" if dxy_chg < -0.1 else "neutral")
        fg = fear_greed_index()
        if fg is not None:
            self.ctx.fear_greed_index = fg
        self.refresh_regime()

    def _fred_pct_change(self, series_id: str) -> float | None:
        """Percent change of the two most recent observations of a FRED series
        (e.g. DTWEXBGS dollar index, DEXJPUS, DEXUSEU). Free, daily, reliable —
        used to revive the FX/DXY signals the rule engine already implements."""
        try:
            obs = self.fred.latest(series_id, 2)
            if len(obs) >= 2:
                cur, prev = float(obs[0]["value"]), float(obs[1]["value"])
                if prev:
                    return (cur - prev) / prev * 100.0
        except Exception:
            pass
        return None

    def refresh_regime(self):
        """Daily: refit (weekly cadence) and classify the market regime from SPY
        history via the GMM; rule-based classify_regime() remains the per-scan
        fallback whenever the model path is unavailable."""
        try:
            candles = self.finnhub.candles("SPY", days=560)
            if not candles or candles.get("s") != "ok":
                return
            df = pd.DataFrame({k: candles[k[0]] for k in
                               ("open", "high", "low", "close", "volume")})
            self.regime_model.maybe_refit(df)
            r = self.regime_model.classify_frame(df, self.ctx.vix)
            self.ctx.regime = r["regime"]
            self.ctx.regime_detail = r["detail"]
            self.ctx.regime_confidence = r["confidence"]
            self.ctx.regime_source = r["source"]
            db.log_event("info", "regime",
                         f"{r['regime']}{('/' + r['detail']) if r['detail'] else ''} "
                         f"({r['source']}, p={r['confidence']})")
        except Exception:
            db.log_event("warn", "regime", "refresh failed; rules fallback in scan loop")

    def refresh_foreign(self):
        moves = {}
        for sym in self.cfg["trading"]["foreign_proxies"]:
            try:
                q = self.finnhub.quote(sym)
                if q and q.get("dp") is not None:
                    moves[sym] = q["dp"]
            except Exception:
                continue
        # FX from FRED (DEXJPUS = JPY per USD, DEXUSEU = USD per EUR). DEXJPUS up
        # means USD/JPY up; DEXUSEU up means EUR/USD up. These revive the
        # usdjpy_riskoff / eurusd_riskoff overnight overlays.
        fx = {}
        jpy = self._fred_pct_change("DEXJPUS")
        eur = self._fred_pct_change("DEXUSEU")
        if jpy is not None:
            fx["USDJPY"] = jpy
        if eur is not None:
            fx["EURUSD"] = eur
        sigs, bias, _ = foreign_overnight_signals(moves, fx)
        self.ctx.overnight_bias = bias
        self.ctx.usdjpy_trend = ("bullish" if (jpy or 0) > 0 else "bearish"
                                 if (jpy or 0) < 0 else "neutral")
        self.ctx.overnight_summary = overnight_summary_sentence(bias, moves, sigs)
        self.on_event("foreign", {"moves": moves, "bias": bias,
                                  "summary": self.ctx.overnight_summary})
        return sigs

    # ------------------------------------------------------------------ snapshot building

    def build_snapshot(self, symbol: str) -> SymbolSnapshot | None:
        candles = self.finnhub.candles(symbol)
        if not candles or candles.get("s") != "ok":
            return None
        df = pd.DataFrame({k: candles[k[0]] for k in
                           ("open", "high", "low", "close", "volume")})
        ind = compute_indicator_frame(df).iloc[-1]
        q = self.finnhub.quote(symbol)
        price = q.get("c") or float(ind.close)

        # IV proxy: 20d realized vol scaled, plus VIX for index ETFs. With the
        # Alpaca indicative options feed wired in (alpaca-py OptionDataClient),
        # replace this with true chain IV.
        rets = df.close.pct_change().dropna().tail(20)
        rv = float(rets.std() * (252 ** 0.5)) if len(rets) else 0.2
        hist_rv = (df.close.pct_change().rolling(20).std() * (252 ** 0.5)).dropna().tolist()
        ivr = compute_iv_rank(rv, hist_rv[-252:]) if hist_rv else 50.0

        news = self.finnhub.company_news(
            symbol, (dt.date.today() - dt.timedelta(days=3)).isoformat(),
            dt.date.today().isoformat())
        sent = self.sentiment.score([n.get("headline", "") for n in news[:12]])

        flow_dir, flow_prem = "neutral", 0.0
        for f in self.flow.fetch():
            if f.get("symbol") == symbol:
                flow_prem = max(flow_prem, f.get("premium", 0))
                flow_dir = f.get("direction", "neutral")

        gap = ((q.get("o", price) - q.get("pc", price)) / q.get("pc", price) * 100
               if q.get("pc") else None)
        return SymbolSnapshot(
            symbol=symbol, price=price, ind=ind, iv_rank=ivr,
            iv_history=hist_rv[-252:],
            earnings_in_days=self._safe_earnings(symbol),
            unusual_flow_premium=flow_prem, unusual_flow_direction=flow_dir,
            news_sentiment=sent, open_gap_pct=gap,
            rs_rank=self._rel_strength_rank(df),
            intraday_above_vwap=(price > float(ind.sma20) if pd.notna(ind.sma20) else None),
        )

    def _rel_strength_rank(self, df: pd.DataFrame, lookback: int = 63) -> float:
        """Relative strength vs SPY as a 0-100 rank (revives canslim_breakout,
        which needs > 85). Maps the symbol's 3-month return minus SPY's onto a
        bounded scale; SPY benchmark is cached per scan."""
        if self._spy_ret63 is None or len(df) <= lookback:
            return 50.0
        try:
            sym_ret = float(df.close.iloc[-1] / df.close.iloc[-(lookback + 1)] - 1.0)
        except Exception:
            return 50.0
        excess = sym_ret - self._spy_ret63
        return round(max(0.0, min(100.0, 50.0 + excess * 500.0)), 1)

    def _safe_earnings(self, symbol: str):
        try:
            return self.finnhub.earnings_in_days(symbol)
        except Exception:
            return None

    def _refresh_spy_benchmark(self, lookback: int = 63):
        """Cache SPY's trailing return once per scan as the relative-strength
        benchmark (candles are TTL-cached, so this is ~free)."""
        try:
            candles = self.finnhub.candles("SPY")
            closes = (candles or {}).get("c") or []
            if len(closes) > lookback:
                self._spy_ret63 = float(closes[-1] / closes[-(lookback + 1)] - 1.0)
        except Exception:
            pass

    # ------------------------------------------------------------------ scan + execute

    def scan_once(self):
        now = self.now_et()
        if self.ctx.regime_source not in ("model", "override"):
            self.ctx.regime = classify_regime(self.ctx.vix, 25.0)
        self._refresh_spy_benchmark()
        macro_sigs = macro_signals(self.ctx)
        weights = db.get_signal_weights()
        open_positions = db.open_trades()
        deployed = sum(t["cost"] or 0 for t in open_positions)
        equity = self.ledger.total() + deployed

        try:
            self.guards.check_drawdown(equity, now.replace(tzinfo=None))
        except RiskRejection as e:
            db.log_event("warn", "halt", str(e))
            self.on_event("halt", {"reason": str(e)})
            return

        for symbol in self.cfg["trading"]["watchlist"]:
            try:
                snap = self.build_snapshot(symbol)
                if snap is None:
                    continue
                if self.ctx.regime_source not in ("model", "override"):
                    self.ctx.regime = classify_regime(self.ctx.vix,
                                                      float(snap.ind.adx14 or 25))
                fired = technical_and_options_rules(snap, self.ctx, now) + macro_sigs
                from ml_engine import build_market_state
                comp = score_setup(fired, build_market_state(snap, self.ctx),
                                   signal_weights=weights)
                for s in fired:
                    db.record_live_signal(symbol, s, None)
                # Confluence gate: the composite floor is 0.30, so a lone rule
                # always clears a bare 0.30 threshold. Require at least two fired
                # signals spanning at least two categories before committing
                # capital — single-signal setups are logged but not traded.
                categories = {s.category for s in fired}
                if comp["strength"] < 0.40 or len(fired) < 2 or len(categories) < 2:
                    continue

                cand = select_strategy(symbol, comp, snap, self.profile, self.ctx, now)
                if cand is None:
                    continue
                # Persisted with the trade so the ML training set is reconstructable
                # (and so the dashboard can compare ml vs rules performance).
                cand.scoring_method = comp.get("scoring_method", "rules")
                cand.ml_score = comp.get("ml_score")
                cand.feature_snapshot = (json.dumps(comp["features"])
                                         if comp.get("features") else None)
                from strategies import affordable_fallback
                prelim = compute_position_size(
                    self.ledger.available(), self.profile, cand.signal_strength,
                    cand.strategy_type) * self.guards.size_multiplier()
                cand = affordable_fallback(cand, snap, self.profile, comp,
                                           self.ctx, now, prelim)
                self.execute_candidate(cand, open_positions, deployed, equity)
                open_positions = db.open_trades()
                deployed = sum(t["cost"] or 0 for t in open_positions)
            except RiskRejection as e:
                db.log_event("info", "risk_reject", f"{symbol}: {e}")
            except Exception:
                db.log_event("error", "scan", f"{symbol}: {traceback.format_exc(limit=2)}")

    def execute_candidate(self, cand, open_positions, deployed, equity):
        with self._lock:
            cash = self.ledger.available()
            mult = self.guards.size_multiplier()
            # `normal` is the UNHALVED profile size — it's what overrides.evaluate
            # expects as the baseline and what the tier math multiplies. The guard
            # multiplier is applied exactly once, at the end (see #7 in the audit).
            normal = compute_position_size(cash, self.profile, cand.signal_strength,
                                           cand.strategy_type)

            ov = self.overrides.evaluate(cand, self.ctx, normal)
            self._drain_rejected_log()
            if ov and ov.requires_approval and ov.approved is None:
                # Approval-gated tier (ALL_IN, or a manually-gated STRONG/MAX):
                # surface the modal and stash the full execution context so the
                # API approval path — or the expiry sweep — can actually submit
                # the trade. (Previously the candidate was discarded here.)
                self._pending_ovr[ov.approval_id] = {
                    "ov": ov, "cand": cand, "normal": normal}
                self.on_event("override_pending", {
                    "approval_id": ov.approval_id, "tier": ov.tier,
                    "symbol": cand.symbol, "strategy": cand.strategy,
                    "normal_size": normal, "criteria": ov.criteria,
                    "expires_at": ov.expires_at.isoformat()})
                db.log_event("warn", "override_pending",
                             f"{ov.tier} on {cand.symbol} awaiting approval {ov.approval_id}")
                return  # submitted on approve()/expiry via execute_pending()

            sized = compute_position_size(cash, self.profile, cand.signal_strength,
                                          cand.strategy_type, conviction_override=ov) * mult
            self._finalize_and_submit(cand, sized, normal, ov, open_positions,
                                      deployed, equity, auto=True)

    def _drain_rejected_log(self):
        """Flush in-memory override rejections to the DB exactly once each (was
        re-inserting the last three on every evaluation → duplicate rows)."""
        while self.overrides.rejected_log:
            db.log_rejected_override(self.overrides.rejected_log.pop(0))

    def _finalize_and_submit(self, cand, sized, normal, ov, open_positions,
                             deployed, equity, auto: bool):
        contracts = contracts_for_dollars(
            cand, sized, UNIVERSAL_HARD_CAPS["absolute_max_single_options_contract_spend"])
        if contracts < 1:
            raise RiskRejection(
                f"Sized ${sized:,.0f} buys 0 contracts of {cand.symbol} "
                f"{cand.strategy} (per-contract ${cand.collateral_per_contract:,.0f}).")
        self.guards.check_capacity(open_positions, deployed,
                                   equity, cand.collateral_per_contract * contracts,
                                   cand.symbol)
        text = (override_rationale(cand, ov, sized, auto) if ov and ov.is_valid()
                else normal_rationale(cand, sized, self.profile, self.ctx.regime,
                                      self.guards.size_multiplier() < 1))
        cand.rationale = llm_polish(text, self.cfg["trading"]["rationale_llm_enabled"])

        order = self.broker.submit_candidate(cand, contracts, self.ledger)
        trade_id = db.insert_trade(cand, contracts, order["cost"], sized,
                                   self.profile["name"], self.ctx.regime,
                                   cand.rationale, ov if (ov and ov.is_valid()) else None)
        if ov and ov.is_valid():
            self.overrides.record_execution(ov)
        self._open_meta[trade_id] = {"opened_at": self.now_et(), "candidate": cand}
        self.on_event("trade_executed", {"trade_id": trade_id, "symbol": cand.symbol,
                                         "strategy": cand.strategy, "cost": order["cost"],
                                         "override": ov.tier if ov and ov.is_valid() else None})
        db.log_event("info", "trade", f"Opened #{trade_id} {cand.symbol} {cand.strategy} "
                                      f"${order['cost']:,.0f}")

    def execute_pending(self, approval_id: str, approved: bool):
        """Resolve a queued approval and actually submit the trade. Called by
        app.py (user approve/skip) and by the expiry sweep (approved=False →
        normal size). Returns a small result dict, or None if unknown/expired.

        Re-validates sizing against fresh cash/positions at execution time, so a
        few minutes in the modal can't oversize the trade. Always emits
        override_resolved so the dashboard modal and the API payload cache clear.
        """
        with self._lock:
            ctx = self._pending_ovr.pop(approval_id, None)
            if ctx is None:
                return None
            ov, cand, normal = ctx["ov"], ctx["cand"], ctx["normal"]
            ov.approved = bool(approved)
            self.overrides.pending.pop(approval_id, None)

            cash = self.ledger.available()
            mult = self.guards.size_multiplier()
            open_positions = db.open_trades()
            deployed = sum(t["cost"] or 0 for t in open_positions)
            equity = self.ledger.total() + deployed

            use_override = approved and ov.is_valid()
            sized = compute_position_size(
                cash, self.profile, cand.signal_strength, cand.strategy_type,
                conviction_override=ov if use_override else None) * mult

            result = {"approval_id": approval_id, "approved": use_override,
                      "tier": ov.tier, "symbol": cand.symbol}
            try:
                self._finalize_and_submit(
                    cand, sized, normal, ov if use_override else None,
                    open_positions, deployed, equity, auto=not use_override)
                result["executed"] = True
            except RiskRejection as e:
                db.log_event("info", "risk_reject", f"{cand.symbol} (override resolve): {e}")
                result["executed"] = False
                result["reason"] = str(e)
            except Exception:
                db.log_event("error", "override_resolve",
                             traceback.format_exc(limit=2))
                result["executed"] = False
            self.on_event("override_resolved",
                          {"approval_id": approval_id, "approved": use_override})
            return result

    # ------------------------------------------------------------------ position management

    def manage_positions(self):
        now = self.now_et()
        today = now.date()
        fc = self.cfg["trading"].get("zero_dte_force_close_et", "15:00")
        try:
            fh, fm = (int(x) for x in fc.split(":"))
        except Exception:
            fh, fm = 15, 0
        force_close_0dte = now.time() >= dt.time(fh, fm)
        profit_target = self.cfg["trading"].get("credit_trade_profit_target_pct", 0.5)
        stop = self.profile["single_position_stop_pct"]

        for t in db.open_trades():
            meta = self._open_meta.get(t["id"])
            opened = (meta["opened_at"] if meta
                      else dt.datetime.fromisoformat(t["ts_open"]).astimezone(ET))
            held_min = (now - opened).total_seconds() / 60
            min_hold = self.cfg["trading"]["min_hold_minutes_options"]
            try:
                expiry = dt.date.fromisoformat((t["expiry"] or "")[:10])
            except Exception:
                expiry = None
            expired = expiry is not None and expiry <= today
            # The minimum-hold rule never blocks an expiry/force-close exit.
            if (t["strategy_type"] != "equity" and held_min < min_hold
                    and not expired and not (t["strategy_type"] == "0dte" and force_close_0dte)):
                continue
            try:
                q = self.finnhub.quote(t["symbol"])
                underlying = q.get("c") or 0
            except Exception:
                continue

            mv = self._position_mark(t, underlying, today)
            if mv is None:
                # Couldn't reprice (no price/legs). Still force a settle if the
                # contract has expired so dead positions don't pin capital forever.
                if not expired:
                    continue
                mv = {"pnl": 0.0, "is_credit": (t["entry_price"] or 0) < 0,
                      "close_cost": 0.0, "credit_received": 0.0,
                      "mark_per_contract": 0.0}
            pnl, is_credit = mv["pnl"], mv["is_credit"]
            entry_cost = t["cost"] or 1

            reason = None
            if expired:
                reason = "Expired — settled at intrinsic"
            elif t["strategy_type"] == "0dte" and force_close_0dte:
                reason = "0DTE force-close at deadline"
            elif is_credit:
                if pnl >= profit_target * (mv["credit_received"] or 0) and mv["credit_received"]:
                    reason = f"Profit target: {profit_target:.0%} of credit captured"
                elif pnl <= -stop * (t["max_loss"] or entry_cost):
                    reason = f"Stop: credit loss {abs(pnl)/max(t['max_loss'] or entry_cost,1):.0%} of max"
            else:
                if pnl <= -stop * entry_cost:
                    reason = f"Stop: down {abs(pnl)/entry_cost:.0%} of debit (limit {stop:.0%})"
                elif t["strategy_type"] == "equity" and pnl <= -0.08 * entry_cost:
                    reason = "Equity stop: 8% below entry (O'Neil rule)"
            if not reason:
                continue

            with self._lock:
                proceeds = max((t["cost"] or 0) + pnl, 0.0)
                try:
                    self.broker.close_trade_position(t, self.ledger,
                                                     proceeds_estimate=proceeds)
                except Exception:
                    db.log_event("error", "close",
                                 f"#{t['id']} broker close failed: {traceback.format_exc(limit=2)}")
                    continue
                db.close_trade(t["id"], mv["mark_per_contract"], pnl)
                lesson = learning.post_trade_analysis({**t, "pnl": pnl})
            self.on_event("trade_closed", {"trade_id": t["id"], "pnl": pnl,
                                           "reason": reason, "lesson": lesson})
            db.log_event("info", "close",
                         f"Closed #{t['id']} {t['symbol']}: {reason} (P&L ${pnl:,.0f})")

    @staticmethod
    def _position_mark(trade: dict, underlying_price: float,
                       today: dt.date) -> dict | None:
        """Reprice the stored legs with Black-Scholes to get a live mark and P&L.
        Interim solution (no options-data API): uses the entry IV proxy and the
        current underlying, which is enough to make stops, profit targets, and the
        learning loop function instead of holding every position at cost forever.

        Returns {pnl, is_credit, close_cost, credit_received, mark_per_contract}
        in dollars, or None if it can't be priced (missing quote/legs)."""
        if not underlying_price:
            return None
        try:
            legs = json.loads(trade.get("legs_json") or "[]")
        except Exception:
            legs = []
        if not legs:
            return None
        iv = trade.get("entry_iv") or 0.0
        if not iv or iv <= 0:
            iv = 0.25  # legacy trades without a stored entry IV → rough default
        contracts = max(trade.get("contracts") or 1, 1)
        r = 0.043

        # Cost (price points/contract) to CLOSE: buy-legs are sold (we receive),
        # sell-legs are bought back (we pay).
        close_cost = 0.0
        for leg in legs:
            try:
                exp = dt.date.fromisoformat(str(leg["expiry"])[:10])
                tau = max((exp - today).days, 0) / 365.0
                p = bs_price(float(underlying_price), float(leg["strike"]), tau, r,
                             iv, leg["kind"])
            except Exception:
                return None
            ratio = leg.get("ratio", 1) or 1
            close_cost += (-p if leg["side"] == "buy" else p) * ratio
        close_cost_dollars = close_cost * 100 * contracts
        is_credit = (trade.get("entry_price") or 0) < 0

        if is_credit:
            credit_received = -(trade.get("entry_price") or 0) * 100 * contracts
            pnl = credit_received - close_cost_dollars
            mark_per_contract = close_cost  # net debit to close, per contract
        else:
            credit_received = 0.0
            current_value = -close_cost_dollars  # received on close
            pnl = current_value - (trade.get("cost") or 0)
            mark_per_contract = -close_cost  # net value, per contract
        return {"pnl": round(pnl, 2), "is_credit": is_credit,
                "close_cost": round(close_cost_dollars, 2),
                "credit_received": round(credit_received, 2),
                "mark_per_contract": round(mark_per_contract, 4)}

    # ------------------------------------------------------------------ main loop

    def run_forever(self):
        db.log_event("info", "boot", f"Bot started. Profile={self.profile['name']}, "
                                     f"settled cash=${self.ledger.available():,.2f}")
        self.refresh_macro()
        last_day = None
        while True:
            try:
                now = self.now_et()
                phase = self.market_phase(now)
                if now.date() != last_day:
                    last_day = now.date()
                    self.overrides.reset_day()
                    if now.weekday() == 0:
                        self.overrides.reset_week()
                    eq = self.ledger.total() + sum(
                        t["cost"] or 0 for t in db.open_trades())
                    # Recover THIS week's Monday-open equity (persisted in the
                    # equity curve) so the weekly-loss guard compares against the
                    # real week start — not today's equity — and survives restarts.
                    monday = (now.date() - dt.timedelta(days=now.weekday())).isoformat()
                    week_eq = db.first_equity_on_or_after(monday) or eq
                    self.guards.start_day(eq, week_start_equity=week_eq)
                    self.broker.sync_ledger(self.ledger)
                    self.refresh_macro()
                    try:                      # weekly ML retrain check (spec §2.2.4)
                        from retrain_worker import maybe_retrain
                        threading.Thread(target=maybe_retrain, daemon=True,
                                         name="ml-retrain").start()
                    except Exception:
                        pass

                for ov in self.overrides.expire_stale():
                    db.log_event("warn", "override_expired",
                                 f"{ov.tier} approval expired → reverting to normal sizing")
                    if ov.approval_id in self._pending_ovr:
                        self.execute_pending(ov.approval_id, approved=False)

                t = time.monotonic()
                if phase == "pre" and t - self._last_scan > 600:
                    self.refresh_foreign()
                    self._last_scan = t
                elif phase == "open":
                    if t - self._last_scan > self.cfg["trading"]["scan_interval_minutes"] * 60:
                        self.scan_once()
                        self._last_scan = t
                        deployed = sum(x["cost"] or 0 for x in db.open_trades())
                        db.snapshot_equity(self.ledger.total() + deployed,
                                           self.ledger.available(), deployed)
                    if t - self._last_manage > \
                            self.cfg["trading"]["position_check_interval_seconds"]:
                        self.manage_positions()
                        self._last_manage = t
                    if now.weekday() == 4 and now.time() >= dt.time(16, 30):
                        pass  # handled in 'after'
                elif phase == "after" and now.weekday() == 4 \
                        and dt.time(16, 30) <= now.time() <= dt.time(16, 40):
                    learning.generate_weekly_report(now.date())
                    time.sleep(600)
                time.sleep(5)
            except KeyboardInterrupt:
                break
            except Exception:
                db.log_event("error", "loop", traceback.format_exc(limit=3))
                time.sleep(15)
