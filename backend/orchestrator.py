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
from options_math import implied_vol, iv_rank as compute_iv_rank
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
        self._last_scan = self._last_manage = 0.0
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
        fg = fear_greed_index()
        if fg is not None:
            self.ctx.fear_greed_index = fg
        self.refresh_regime()

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
        moves, fx = {}, {}
        for sym in self.cfg["trading"]["foreign_proxies"]:
            try:
                q = self.finnhub.quote(sym)
                if q and q.get("dp") is not None:
                    moves[sym] = q["dp"]
            except Exception:
                continue
        sigs, bias, _ = foreign_overnight_signals(moves, fx)
        self.ctx.overnight_bias = bias
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
            intraday_above_vwap=(price > float(ind.sma20) if pd.notna(ind.sma20) else None),
        )

    def _safe_earnings(self, symbol: str):
        try:
            return self.finnhub.earnings_in_days(symbol)
        except Exception:
            return None

    # ------------------------------------------------------------------ scan + execute

    def scan_once(self):
        now = self.now_et()
        if self.ctx.regime_source not in ("model", "override"):
            self.ctx.regime = classify_regime(self.ctx.vix, 25.0)
        macro_sigs = macro_signals(self.ctx)
        weights = db.get_signal_weights()
        open_positions = db.open_trades()
        deployed = sum(t["cost"] or 0 for t in open_positions)
        equity = self.ledger.available() + deployed

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
                if comp["strength"] < 0.30 or not fired:
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
        cash = self.ledger.available()
        normal = compute_position_size(cash, self.profile, cand.signal_strength,
                                       cand.strategy_type) * self.guards.size_multiplier()

        ov = self.overrides.evaluate(cand, self.ctx, normal)
        for rec in self.overrides.rejected_log[-3:]:
            db.log_rejected_override(rec)
        if ov and ov.requires_approval and ov.approved is None:
            # ALL_IN (or manually-gated tier): surface modal, wait via API. The
            # pending queue is polled by app.py; if it expires we trade normal size.
            self.on_event("override_pending", {
                "approval_id": ov.approval_id, "tier": ov.tier,
                "symbol": cand.symbol, "strategy": cand.strategy,
                "normal_size": normal, "criteria": ov.criteria,
                "expires_at": ov.expires_at.isoformat()})
            db.log_event("warn", "override_pending",
                         f"{ov.tier} on {cand.symbol} awaiting approval {ov.approval_id}")
            return  # executed on approval (app.py) or as normal size on expiry

        sized = compute_position_size(cash, self.profile, cand.signal_strength,
                                      cand.strategy_type, conviction_override=ov)
        sized *= self.guards.size_multiplier()
        self._finalize_and_submit(cand, sized, normal, ov, open_positions,
                                  deployed, equity, auto=True)

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

    def resolve_pending_override(self, approval_id: str, approved: bool):
        """Called by app.py from the dashboard modal."""
        ov = self.overrides.resolve(approval_id, approved)
        if not ov:
            return None
        # Re-fetch sizing context fresh; pending candidates are stored on the event
        # consumer side (app keeps the payload). Approval expiry → normal size.
        return ov

    # ------------------------------------------------------------------ position management

    def manage_positions(self):
        now = self.now_et()
        force_close_0dte = now.time() >= dt.time(15, 0)
        for t in db.open_trades():
            meta = self._open_meta.get(t["id"])
            opened = (meta["opened_at"] if meta
                      else dt.datetime.fromisoformat(t["ts_open"]).astimezone(ET))
            held_min = (now - opened).total_seconds() / 60
            if t["strategy_type"] != "equity" and held_min < \
                    self.cfg["trading"]["min_hold_minutes_options"]:
                continue
            try:
                q = self.finnhub.quote(t["symbol"])
                mark = self._estimate_mark(t, q.get("c") or 0)
            except Exception:
                continue

            entry_cost = t["cost"] or 1
            pnl = mark - entry_cost
            stop = self.profile["single_position_stop_pct"]
            is_credit = (t["entry_price"] or 0) < 0
            reason = None
            if t["strategy_type"] == "0dte" and force_close_0dte:
                reason = "0DTE force-close at 3:00pm ET"
            elif not is_credit and pnl <= -stop * entry_cost:
                reason = f"Stop: down {abs(pnl)/entry_cost:.0%} of debit (limit {stop:.0%})"
            elif is_credit and mark >= entry_cost + 0.5 * (t["max_profit"] or 0):
                reason = "Profit target: 50% of max credit captured"
            elif t["strategy_type"] == "equity" and pnl <= -0.08 * entry_cost:
                reason = "Equity stop: 8% below entry (O'Neil rule)"
            if reason:
                self.broker.close_position(t["symbol"], self.ledger,
                                           proceeds_estimate=max(mark, 0))
                db.close_trade(t["id"], mark / max(t["contracts"] or 1, 1), pnl)
                lesson = learning.post_trade_analysis({**t, "pnl": pnl})
                self.on_event("trade_closed", {"trade_id": t["id"], "pnl": pnl,
                                               "reason": reason, "lesson": lesson})
                db.log_event("info", "close", f"Closed #{t['id']} {t['symbol']}: {reason}")

    @staticmethod
    def _estimate_mark(trade: dict, underlying_price: float) -> float:
        """Conservative mark using underlying drift vs entry. Replace with real
        option marks once the Alpaca options data client is wired in."""
        cost = trade["cost"] or 0
        ref = trade.get("sized_dollars") or cost
        if not underlying_price or not ref:
            return cost
        drift = 0.0  # without entry underlying stored, hold mark at cost
        return cost * (1 + drift)

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
                    eq = self.ledger.available() + sum(
                        t["cost"] or 0 for t in db.open_trades())
                    self.guards.start_day(eq)
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

                t = time.monotonic()
                if phase == "pre" and t - self._last_scan > 600:
                    self.refresh_foreign()
                    self._last_scan = t
                elif phase == "open":
                    if t - self._last_scan > self.cfg["trading"]["scan_interval_minutes"] * 60:
                        self.scan_once()
                        self._last_scan = t
                        deployed = sum(x["cost"] or 0 for x in db.open_trades())
                        db.snapshot_equity(self.ledger.available() + deployed,
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
