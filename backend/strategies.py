"""
strategies.py — Strategy playbook (spec §2.3) and the Layer-3 selector.

Cash-account deviations from the spec playbook (documented in README):
  * Short strangle → iron condor (naked legs impossible in a cash account).
  * Jade lizard → dropped (naked call leg); its neutral-bullish slot is covered
    by cash-secured puts and call credit spreads inside iron condors.
  * SPX/SPXW → SPY/QQQ (Alpaca has no index options).
PMCC is kept but requires options level 3 on the account.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

from options_math import (bs_greeks, expected_move, occ_symbol, strike_for_delta,
                          strike_increment_for)
from signals import BEARISH, BULLISH, NEUTRAL, MarketContext, Signal

R = 0.043  # risk-free rate assumption for greeks; refreshed from FRED DFF at runtime


@dataclass
class OptionLeg:
    side: str          # buy | sell
    kind: str          # call | put
    strike: float
    expiry: dt.date
    symbol: str = ""
    ratio: int = 1

    def __post_init__(self):
        if not self.symbol:
            self.symbol = ""  # filled by build() with the underlying root


@dataclass
class TradeCandidate:
    symbol: str
    strategy: str                  # playbook key
    strategy_type: str             # sizing bucket: long_options | spread | 0dte | equity
                                   # (credit strategies use 'spread' for sizing,
                                   #  'short_premium' recorded separately)
    direction: str
    legs: list = field(default_factory=list)
    shares: int = 0
    dte: int = 0
    expiry: Optional[dt.date] = None
    est_debit: float = 0.0         # per-spread debit (or credit, negative) per contract
    est_credit: float = 0.0
    spread_width: float = 0.0
    max_profit: float = 0.0        # per contract, dollars
    max_loss: float = 0.0
    collateral_per_contract: float = 0.0
    signal_strength: float = 0.0
    signals: list = field(default_factory=list)
    iv_rank: float = 50.0
    entry_iv: float = 0.0           # annualized vol used to price the legs at entry
    catalyst: Optional[str] = None
    # conviction-criteria inputs
    technical_direction: str = NEUTRAL
    options_flow_direction: str = NEUTRAL
    macro_direction: str = NEUTRAL
    sentiment_direction: str = NEUTRAL
    unusual_options_premium: float = 0.0
    backtest_win_rate: Optional[float] = None
    backtest_sample_size: Optional[int] = None
    rationale: str = ""

    @property
    def is_credit(self) -> bool:
        return self.est_credit > 0


PLAYBOOK = {
    "long_call":          {"type": "long_options", "needs": "bullish, IV rank < ~45"},
    "long_put":           {"type": "long_options", "needs": "bearish, IV rank < ~45"},
    "bull_call_spread":   {"type": "spread", "needs": "moderately bullish, IV 30-60"},
    "bear_put_spread":    {"type": "spread", "needs": "moderately bearish, IV 30-60"},
    "cash_secured_put":   {"type": "spread", "needs": "bullish, IV rank > 50, no earnings"},
    "covered_call":       {"type": "spread", "needs": "own 100 shares, IV rank > 40"},
    "iron_condor":        {"type": "spread", "needs": "range-bound, IV rank > 60"},
    "iron_butterfly":     {"type": "spread", "needs": "pin conviction, IV rank > 70"},
    "calendar_spread":    {"type": "spread", "needs": "steep vol term structure"},
    "long_straddle":      {"type": "long_options", "needs": "big move expected, IV rank < 50"},
    "pmcc":               {"type": "spread", "needs": "LEAPS + short weekly; level 3"},
    "0dte_debit_spread":  {"type": "0dte", "needs": "intraday trend, SPY/QQQ"},
    "0dte_iron_condor":   {"type": "0dte", "needs": "flat open, VIX < 18, SPY/QQQ"},
    "equity_momentum":    {"type": "equity", "needs": "52w high breakout, RS > 85"},
    "equity_mean_reversion": {"type": "equity", "needs": "RSI < 30 near 52w low"},
}


def next_expiry_in_range(dte_lo: int, dte_hi: int, today: Optional[dt.date] = None) -> tuple[dt.date, int]:
    """Pick the Friday expiry closest to the middle of the profile's DTE window."""
    today = today or dt.date.today()
    target = today + dt.timedelta(days=(dte_lo + dte_hi) // 2)
    d = target
    while d.weekday() != 4:  # Friday
        d += dt.timedelta(days=1)
    dte = (d - today).days
    if dte < dte_lo:
        d += dt.timedelta(days=7)
        dte = (d - today).days
    return d, dte


def select_strategy(symbol: str, composite: dict, snapshot, profile: dict,
                    ctx: MarketContext, now_et: Optional[dt.datetime] = None) -> Optional[TradeCandidate]:
    """Layer 3: signal combination → strategy + strikes + expiry. Returns None if
    nothing in the playbook fits the profile's permissions and IV regime."""
    direction, strength = composite["direction"], composite["strength"]
    fired: list[Signal] = composite["signals"]
    iv_rank = snapshot.iv_rank
    hints = [s.strategy_hint for s in fired if s.strategy_hint]

    def hinted(name):
        return name in hints

    chosen: Optional[str] = None

    # 0DTE first — these are time-windowed opportunities.
    if profile["allow_0dte"]:
        if hinted("0dte_iron_condor"):
            chosen = "0dte_iron_condor"
        elif hinted("0dte_debit_spread") and direction in (BULLISH, BEARISH):
            chosen = "0dte_debit_spread"

    earnings_soon = snapshot.earnings_in_days is not None and snapshot.earnings_in_days <= 14
    if chosen is None and earnings_soon and not profile["allow_earnings_plays"]:
        # conservative: skip names inside the earnings window entirely
        return None

    if chosen is None:
        if hinted("iron_condor") and iv_rank > profile["min_iv_rank_for_short_options"]:
            chosen = "iron_condor"
        elif hinted("cash_secured_put") and iv_rank > profile["min_iv_rank_for_short_options"]:
            chosen = "cash_secured_put"
        elif direction == BULLISH:
            if hinted("equity_momentum"):
                chosen = "equity_momentum"
            elif iv_rank <= profile["max_iv_rank_for_long_options"]:
                chosen = "long_call" if strength >= 0.7 else "bull_call_spread"
            elif iv_rank <= 60 or profile.get("allow_high_iv_long_options"):
                chosen = "bull_call_spread"
        elif direction == BEARISH:
            if iv_rank <= profile["max_iv_rank_for_long_options"]:
                chosen = "long_put" if strength >= 0.7 else "bear_put_spread"
            elif iv_rank <= 60 or profile.get("allow_high_iv_long_options"):
                chosen = "bear_put_spread"
        elif direction == NEUTRAL and earnings_soon and iv_rank < 50 \
                and profile["allow_earnings_plays"]:
            chosen = "long_straddle"

    if chosen is None:
        return None
    return build_candidate(chosen, symbol, snapshot, profile, composite, ctx, now_et)


def build_candidate(strategy: str, symbol: str, snap, profile: dict, composite: dict,
                    ctx: MarketContext, now_et: Optional[dt.datetime] = None) -> TradeCandidate:
    """Construct legs, strikes, expiry, and risk numbers for the chosen strategy."""
    S = snap.price
    iv = (snap.iv_history[-1] if snap.iv_history else max(ctx.vix, 12) / 100.0)
    inc = strike_increment_for(S)
    is_0dte = strategy.startswith("0dte")
    today = (now_et.date() if now_et else dt.date.today())

    if is_0dte:
        expiry, dte = today, 0
        t = 4 / (24 * 365)  # rough remaining session time for greeks
    else:
        expiry, dte = next_expiry_in_range(*profile["preferred_dte_range"], today=today)
        t = dte / 365.0

    d_lo, d_hi = profile["preferred_delta_long"]
    target_long = (d_lo + d_hi) / 2
    s_lo, s_hi = profile["preferred_delta_short"]
    target_short = (s_lo + s_hi) / 2

    cand = TradeCandidate(
        symbol=symbol, strategy=strategy, strategy_type=PLAYBOOK[strategy]["type"],
        direction=composite["direction"], dte=dte, expiry=expiry,
        signal_strength=composite["strength"], signals=composite["signals"],
        iv_rank=snap.iv_rank, entry_iv=iv,
        unusual_options_premium=snap.unusual_flow_premium,
        catalyst=(f"earnings in {snap.earnings_in_days}d"
                  if snap.earnings_in_days is not None and snap.earnings_in_days <= 21 else None),
    )

    def leg(side, kind, strike):
        return OptionLeg(side, kind, strike, expiry,
                         symbol=occ_symbol(symbol, expiry, kind, strike))

    def prem(kind, strike):
        from options_math import bs_price
        return max(bs_price(S, strike, t, R, iv, kind), 0.01)

    em = expected_move(S, iv, max(dte, 1))

    if strategy in ("long_call", "long_put"):
        kind = "call" if strategy == "long_call" else "put"
        k = strike_for_delta(S, t, R, iv, kind, target_long, inc)
        p = prem(kind, k)
        cand.legs = [leg("buy", kind, k)]
        cand.est_debit = p
        cand.max_loss = p * 100
        cand.max_profit = p * 100 * 3  # heuristic target for R:R math on long options
        cand.collateral_per_contract = p * 100

    elif strategy in ("bull_call_spread", "bear_put_spread", "0dte_debit_spread"):
        kind = "call" if (cand.direction == BULLISH) else "put"
        k1 = strike_for_delta(S, t, R, iv, kind, 0.50 if is_0dte else target_long, inc)
        width = max(inc, round((em if is_0dte else em / 2) / inc) * inc)
        k2 = k1 + width if kind == "call" else k1 - width
        debit = max(prem(kind, k1) - prem(kind, k2), 0.05)
        cand.legs = [leg("buy", kind, k1), leg("sell", kind, k2)]
        cand.est_debit, cand.spread_width = debit, width
        cand.max_loss = debit * 100
        cand.max_profit = (width - debit) * 100
        cand.collateral_per_contract = debit * 100

    elif strategy in ("iron_condor", "0dte_iron_condor", "iron_butterfly"):
        if strategy == "iron_butterfly":
            pk_s = ck_s = round(S / inc) * inc
        else:
            sd = em if is_0dte else em  # ~1 SD wings (≈16-delta)
            pk_s = round((S - sd) / inc) * inc
            ck_s = round((S + sd) / inc) * inc
        wing = max(inc, round((em / 2) / inc) * inc)
        pk_l, ck_l = pk_s - wing, ck_s + wing
        credit = max((prem("put", pk_s) - prem("put", pk_l))
                     + (prem("call", ck_s) - prem("call", ck_l)), 0.05)
        cand.legs = [leg("sell", "put", pk_s), leg("buy", "put", pk_l),
                     leg("sell", "call", ck_s), leg("buy", "call", ck_l)]
        cand.est_credit, cand.spread_width = credit, wing
        cand.max_profit = credit * 100
        cand.max_loss = (wing - credit) * 100
        # Spec note: reserve full width × 100 as collateral in the cash framework.
        cand.collateral_per_contract = wing * 100

    elif strategy == "cash_secured_put":
        k = strike_for_delta(S, t, R, iv, "put", target_short, inc)
        credit = prem("put", k)
        cand.legs = [leg("sell", "put", k)]
        cand.est_credit = credit
        cand.max_profit = credit * 100
        cand.max_loss = (k - credit) * 100
        cand.collateral_per_contract = k * 100  # fully cash-secured

    elif strategy == "covered_call":
        k = strike_for_delta(S, t, R, iv, "call", target_short, inc)
        credit = prem("call", k)
        cand.legs = [leg("sell", "call", k)]
        cand.est_credit = credit
        cand.max_profit = (k - S + credit) * 100
        cand.max_loss = (S - credit) * 100
        cand.collateral_per_contract = 0.0  # shares cover it

    elif strategy == "long_straddle":
        k = round(S / inc) * inc
        debit = prem("call", k) + prem("put", k)
        cand.legs = [leg("buy", "call", k), leg("buy", "put", k)]
        cand.est_debit = debit
        cand.max_loss = debit * 100
        cand.max_profit = debit * 100 * 2.5
        cand.collateral_per_contract = debit * 100

    elif strategy == "calendar_spread":
        k = round(S / inc) * inc
        far_expiry, far_dte = next_expiry_in_range(28, 35, today=today)
        near_expiry, _ = next_expiry_in_range(5, 9, today=today)
        far_t, near_t = far_dte / 365.0, max((near_expiry - today).days, 1) / 365.0
        from options_math import bs_price
        debit = max(bs_price(S, k, far_t, R, iv, "call")
                    - bs_price(S, k, near_t, R, iv, "call"), 0.05)
        cand.legs = [
            OptionLeg("buy", "call", k, far_expiry, occ_symbol(symbol, far_expiry, "call", k)),
            OptionLeg("sell", "call", k, near_expiry, occ_symbol(symbol, near_expiry, "call", k)),
        ]
        cand.est_debit = debit
        cand.max_loss = debit * 100
        cand.max_profit = debit * 100 * 1.5
        cand.collateral_per_contract = debit * 100
        cand.expiry, cand.dte = far_expiry, far_dte

    elif strategy.startswith("equity_"):
        cand.shares = 1  # sized later by dollars / price
        cand.max_loss = S * 0.08 * 100  # 7-8% stop per O'Neil, per 100 shares
        cand.max_profit = S * 0.20 * 100
        cand.collateral_per_contract = S

    # technical/flow/macro/sentiment directions for the conviction criteria
    by_cat = {}
    for s in composite["signals"]:
        by_cat.setdefault(s.category, []).append(s)

    def cat_dir(cat):
        sigs = by_cat.get(cat, [])
        if not sigs:
            return NEUTRAL
        bull = sum(s.strength for s in sigs if s.direction == BULLISH)
        bear = sum(s.strength for s in sigs if s.direction == BEARISH)
        return BULLISH if bull > bear else BEARISH if bear > bull else NEUTRAL

    cand.technical_direction = cat_dir("technical")
    cand.options_flow_direction = (snap.unusual_flow_direction
                                   if snap.unusual_flow_premium > 0 else cat_dir("options"))
    cand.macro_direction = cat_dir("macro") if by_cat.get("macro") else ctx.overnight_bias
    cand.sentiment_direction = cat_dir("sentiment")
    return cand


def affordable_fallback(cand: TradeCandidate, snap, profile: dict, composite: dict,
                        ctx: MarketContext, now_et, dollars: float) -> TradeCandidate:
    """The spec's core sizing insight in reverse: if the sized dollars can't buy a
    single contract of a long option, downgrade to the cheaper defined-risk debit
    spread instead of skipping the trade."""
    if cand.strategy in ("long_call", "long_put") and cand.collateral_per_contract > dollars:
        alt = "bull_call_spread" if cand.strategy == "long_call" else "bear_put_spread"
        return build_candidate(alt, cand.symbol, snap, profile, composite, ctx, now_et)
    return cand


def contracts_for_dollars(cand: TradeCandidate, dollars: float, max_contract_spend: float) -> int:
    """How many contracts (or shares) the sized dollar amount buys, honoring the
    universal per-contract spend cap."""
    if cand.strategy_type == "equity":
        return int(dollars // cand.collateral_per_contract)
    per = cand.collateral_per_contract
    if per <= 0:
        return 0
    if cand.est_debit * 100 > max_contract_spend:
        return 0
    return int(dollars // per)
