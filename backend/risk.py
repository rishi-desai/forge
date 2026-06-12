"""
risk.py — Risk profiles, universal caps, position sizing, settled-cash ledger, guards.

Implements spec Phase 5. Deliberately stdlib-only so the math is testable offline.

Spec reconciliations (see README "Spec deviations"):
  * The $10,000 universal single-position cap binds EVERYTHING, including all
    conviction-override tiers (spec constraint #5 says it "cannot be overridden";
    the ALL_IN $20k cap and the $12k worked example contradict it and lose).
  * Override caps follow §5.6.1 (profile abs cap × tier multiplier), not the
    inconsistent caps in the worked-example table.
  * Cash accounts were never subject to PDT, but they ARE subject to settled-funds
    rules. SettledCashLedger tracks T+1 settlement so the bot never spends
    unsettled proceeds (good-faith-violation avoidance). The spec omitted this.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

# ----------------------------------------------------------------------------- profiles

CONSERVATIVE = {
    "name": "conservative",
    "max_position_pct": 0.03,
    "max_position_abs_cap": 3000,
    "max_concurrent_positions": 8,
    "max_portfolio_deployed_pct": 0.50,
    "preferred_dte_range": (30, 60),
    "preferred_delta_long": (0.30, 0.45),
    "preferred_delta_short": (0.15, 0.25),
    "max_iv_rank_for_long_options": 35,
    "min_iv_rank_for_short_options": 55,
    "single_position_stop_pct": 0.35,
    "daily_drawdown_halt_pct": 0.03,
    "weekly_loss_reduce_pct": 0.02,
    "allow_naked_options": False,
    "allow_earnings_plays": False,
    "allow_0dte": False,
    "max_0dte_position_pct": 0.0,
    "allow_high_iv_long_options": False,
    "max_correlated_positions": 2,
    "min_option_oi": 1000,
    "max_bid_ask_spread_pct": 0.07,
}

MODERATE = {
    "name": "moderate",
    "max_position_pct": 0.08,
    "max_position_abs_cap": 5000,
    "max_concurrent_positions": 6,
    "max_portfolio_deployed_pct": 0.70,
    "preferred_dte_range": (21, 45),
    "preferred_delta_long": (0.35, 0.55),
    "preferred_delta_short": (0.20, 0.30),
    "max_iv_rank_for_long_options": 45,
    "min_iv_rank_for_short_options": 50,
    "single_position_stop_pct": 0.50,
    "daily_drawdown_halt_pct": 0.05,
    "weekly_loss_reduce_pct": 0.03,
    "allow_naked_options": False,
    "allow_earnings_plays": True,
    "allow_0dte": True,
    "max_0dte_position_pct": 0.02,
    "allow_high_iv_long_options": False,
    "max_correlated_positions": 3,
    "min_option_oi": 500,
    "max_bid_ask_spread_pct": 0.10,
}

AGGRESSIVE = {
    "name": "aggressive",
    "max_position_pct": 0.20,
    "max_position_abs_cap": 8000,
    "max_concurrent_positions": 5,
    "max_portfolio_deployed_pct": 0.90,
    "preferred_dte_range": (7, 30),
    "preferred_delta_long": (0.40, 0.65),
    "preferred_delta_short": (0.25, 0.40),
    "max_iv_rank_for_long_options": 55,
    "min_iv_rank_for_short_options": 45,
    "single_position_stop_pct": 0.65,
    "daily_drawdown_halt_pct": 0.10,
    "weekly_loss_reduce_pct": 0.07,
    "allow_naked_options": False,
    "allow_earnings_plays": True,
    "allow_0dte": True,
    "max_0dte_position_pct": 0.05,
    "allow_high_iv_long_options": True,
    "max_correlated_positions": 4,
    "min_option_oi": 300,
    "max_bid_ask_spread_pct": 0.15,
}

MAX_AGGRESSION = {
    "name": "max_aggression",
    "max_position_pct": 0.40,
    "max_position_abs_cap": 5000,
    "max_concurrent_positions": 3,
    "max_portfolio_deployed_pct": 1.00,
    "preferred_dte_range": (1, 14),
    "preferred_delta_long": (0.45, 0.65),
    "preferred_delta_short": (0.30, 0.50),
    "max_iv_rank_for_long_options": 65,
    "min_iv_rank_for_short_options": 40,
    "single_position_stop_pct": 0.80,
    "daily_drawdown_halt_pct": 0.25,
    "weekly_loss_reduce_pct": None,
    "allow_naked_options": False,
    "allow_earnings_plays": True,
    "allow_0dte": True,
    "max_0dte_position_pct": 0.40,
    "allow_high_iv_long_options": True,
    "max_correlated_positions": 3,
    "min_option_oi": 100,
    "max_bid_ask_spread_pct": 0.20,
    "startup_warning": (
        "MAX_AGGRESSION profile active. Position sizes are intentionally large "
        "relative to account value. Designed for small accounts ($500-$3,000) where "
        "total loss is acceptable. Do not use on an account whose loss would be "
        "financially meaningful to you."
    ),
}

PROFILES = {p["name"]: p for p in (CONSERVATIVE, MODERATE, AGGRESSIVE, MAX_AGGRESSION)}

UNIVERSAL_HARD_CAPS = {
    "absolute_max_single_position": 10_000,
    "absolute_max_single_options_contract_spend": 5_000,
    "account_type": "cash",
    "max_buying_power_usage": 1.00,
    "absolute_min_option_oi": 50,
    "absolute_max_bid_ask_spread_pct": 0.25,
    "reject_if_position_exceeds_cash": True,
}

MIN_SIGNAL_STRENGTH_TO_TRADE = 0.30
CASH_BUFFER = 0.95  # leave 5% cash buffer when capping against available cash

# Rough sector map for the correlation guard.
SECTOR_MAP = {
    "AAPL": "tech", "MSFT": "tech", "NVDA": "tech", "AMD": "tech", "GOOGL": "tech",
    "META": "tech", "AMZN": "consumer", "TSLA": "consumer", "SPY": "index",
    "QQQ": "index", "IWM": "index", "XLE": "energy", "XLF": "financials",
    "EWJ": "foreign", "EWG": "foreign", "EWU": "foreign", "FXI": "foreign",
    "INDA": "foreign", "EWZ": "foreign",
}


def get_profile(name: str, custom_overrides: Optional[dict] = None) -> dict:
    if name not in PROFILES:
        raise ValueError(f"Unknown risk profile '{name}'. Valid: {list(PROFILES)}")
    profile = dict(PROFILES[name])
    if custom_overrides:
        unknown = set(custom_overrides) - set(profile)
        if unknown:
            raise ValueError(f"custom_overrides contains unknown keys: {unknown}")
        profile.update(custom_overrides)
    return profile


# ----------------------------------------------------------------------------- sizing

class InsufficientCashError(Exception):
    pass


class RiskRejection(Exception):
    """Order rejected by a risk rule. Message is the plain-English reason."""


def compute_position_size(
    portfolio_cash: float,
    profile: dict,
    signal_strength: float,
    strategy_type: str,
    conviction_override=None,
) -> float:
    """Spec §5.4. strategy_type ∈ {long_options, spread, 0dte, equity}.

    `conviction_override` is an ApprovedOverride from overrides.py (or None).
    Returns dollars to allocate, already capped by profile, universal cap, and cash.
    """
    if signal_strength < MIN_SIGNAL_STRENGTH_TO_TRADE:
        raise RiskRejection(
            f"Signal strength {signal_strength:.2f} below trade threshold "
            f"{MIN_SIGNAL_STRENGTH_TO_TRADE:.2f}"
        )

    if conviction_override is not None and conviction_override.is_valid():
        return compute_override_size(portfolio_cash, profile, conviction_override)

    base_pct = profile["max_position_pct"]
    signal_scalar = 0.40 + 0.60 * signal_strength  # [0,1] → [0.40, 1.00]

    if strategy_type == "0dte":
        if not profile["allow_0dte"]:
            raise RiskRejection(f"Profile '{profile['name']}' does not allow 0DTE trades")
        strategy_scalar = profile.get("max_0dte_position_pct", 0.03) / base_pct
    else:
        strategy_scalar = {"long_options": 1.00, "spread": 1.00, "equity": 1.20}.get(strategy_type)
        if strategy_scalar is None:
            raise ValueError(f"Unknown strategy_type '{strategy_type}'")

    raw_size = portfolio_cash * base_pct * signal_scalar * strategy_scalar
    capped = min(raw_size, profile["max_position_abs_cap"])
    final = min(capped, UNIVERSAL_HARD_CAPS["absolute_max_single_position"])

    if final > portfolio_cash:
        final = portfolio_cash * CASH_BUFFER
    return round(final, 2)


def compute_override_size(portfolio_cash: float, profile: dict, override) -> float:
    """Spec §5.6.1 with the universal $10k cap applied on top (constraint #5)."""
    tier = override.tier_spec
    if tier.get("size_multiplier") is not None:
        size = override.normal_size * tier["size_multiplier"]
        cap = profile["max_position_abs_cap"] * tier["abs_cap_multiplier"]
    else:  # ALL_IN: fixed % of cash
        size = portfolio_cash * tier["fixed_pct_of_cash"]
        cap = tier["abs_hard_cap"]

    final = min(size, cap, UNIVERSAL_HARD_CAPS["absolute_max_single_position"])
    if final > portfolio_cash:
        final = portfolio_cash * CASH_BUFFER
    return round(final, 2)


# ----------------------------------------------------------------------------- settled cash

def _next_business_day(d: dt.date) -> dt.date:
    d = d + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


@dataclass
class SettledCashLedger:
    """Tracks settled vs unsettled funds in the cash account (T+1 for both
    equities and options as of 2024+). The bot only spends settled cash —
    this is what prevents good-faith violations in a real cash account, and
    we mirror the same discipline in paper so behavior translates 1:1.
    """
    settled: float
    unsettled: list = field(default_factory=list)  # [(settle_date, amount)]

    def available(self, today: Optional[dt.date] = None) -> float:
        self._roll(today or dt.date.today())
        return self.settled

    def spend(self, amount: float, today: Optional[dt.date] = None) -> None:
        self._roll(today or dt.date.today())
        if amount > self.settled + 1e-9:
            raise InsufficientCashError(
                f"Order requires ${amount:,.2f} but only ${self.settled:,.2f} settled cash "
                f"is available. This is a cash account — no margin, no unsettled funds."
            )
        self.settled -= amount

    def receive_proceeds(self, amount: float, trade_date: Optional[dt.date] = None) -> None:
        trade_date = trade_date or dt.date.today()
        self.unsettled.append((_next_business_day(trade_date), amount))

    def _roll(self, today: dt.date) -> None:
        still_pending = []
        for settle_date, amount in self.unsettled:
            if settle_date <= today:
                self.settled += amount
            else:
                still_pending.append((settle_date, amount))
        self.unsettled = still_pending


# ----------------------------------------------------------------------------- order validation

def estimate_order_cost(order: dict) -> float:
    """order: {strategy_type, contracts/shares, premium or net_debit or price,
    spread_width (for credit spreads), net_credit, strike (CSP)}.
    Returns cash that must be reserved.
    """
    t = order["strategy_type"]
    q = order.get("contracts", order.get("shares", 1))
    if t == "equity":
        return order["price"] * q
    if t in ("long_call", "long_put", "long_straddle", "long_strangle", "long_options"):
        return order["premium"] * 100 * q
    if t in ("debit_spread", "spread", "calendar_spread", "pmcc", "0dte_debit_spread"):
        return order["net_debit"] * 100 * q
    if t == "cash_secured_put":
        return order["strike"] * 100 * q  # full cash securing
    if t in ("iron_condor", "iron_butterfly", "credit_spread", "0dte_iron_condor"):
        # Spec note: reserve max loss = spread width × 100 (not net of credit, to be safe).
        return order["spread_width"] * 100 * q
    if t == "covered_call":
        return 0.0  # shares already owned and reserved
    raise ValueError(f"Unknown strategy_type for cost estimation: {t}")


def validate_cash_account_order(order: dict, ledger: SettledCashLedger) -> float:
    """Raises on any violation; returns the estimated cost if valid."""
    cost = estimate_order_cost(order)
    available = ledger.available()
    if cost > available:
        raise InsufficientCashError(
            f"Order requires ${cost:,.2f} but only ${available:,.2f} available. "
            f"This is a cash account — no margin allowed."
        )
    if order.get("is_naked_short"):
        raise RiskRejection("Naked short options are not possible in a cash account.")
    per_contract = order.get("premium", order.get("net_debit", 0)) * 100
    if per_contract > UNIVERSAL_HARD_CAPS["absolute_max_single_options_contract_spend"]:
        raise RiskRejection(
            f"Single contract spend ${per_contract:,.0f} exceeds universal "
            f"${UNIVERSAL_HARD_CAPS['absolute_max_single_options_contract_spend']:,} cap."
        )
    return cost


def validate_liquidity(profile: dict, open_interest: int, bid: float, ask: float) -> None:
    mid = (bid + ask) / 2 if (bid + ask) > 0 else 0
    spread_pct = (ask - bid) / mid if mid > 0 else 1.0
    min_oi = max(profile["min_option_oi"], UNIVERSAL_HARD_CAPS["absolute_min_option_oi"])
    max_spread = min(profile["max_bid_ask_spread_pct"],
                     UNIVERSAL_HARD_CAPS["absolute_max_bid_ask_spread_pct"])
    if open_interest < min_oi:
        raise RiskRejection(f"Open interest {open_interest} below minimum {min_oi}")
    if spread_pct > max_spread:
        raise RiskRejection(f"Bid/ask spread {spread_pct:.1%} exceeds max {max_spread:.1%}")


# ----------------------------------------------------------------------------- portfolio guards

@dataclass
class GuardState:
    day_start_equity: float
    week_start_equity: float
    halted_today: bool = False
    sizes_halved: bool = False


class PortfolioGuards:
    """Daily halt, weekly size reduction, deployment cap, concurrency, correlation."""

    def __init__(self, profile: dict):
        self.profile = profile
        self.state: Optional[GuardState] = None

    def start_day(self, equity: float, week_start_equity: Optional[float] = None) -> None:
        self.state = GuardState(
            day_start_equity=equity,
            week_start_equity=week_start_equity or equity,
        )

    def check_drawdown(self, current_equity: float, now: Optional[dt.datetime] = None) -> None:
        s, p = self.state, self.profile
        daily_dd = (s.day_start_equity - current_equity) / s.day_start_equity
        if daily_dd >= p["daily_drawdown_halt_pct"]:
            s.halted_today = True
            raise RiskRejection(
                f"DAILY HALT: portfolio down {daily_dd:.1%} today "
                f"(limit {p['daily_drawdown_halt_pct']:.0%}). No new trades until tomorrow."
            )
        weekly_limit = p.get("weekly_loss_reduce_pct")
        if weekly_limit is not None and not s.sizes_halved:
            now = now or dt.datetime.now()
            weekly_dd = (s.week_start_equity - current_equity) / s.week_start_equity
            if now.weekday() >= 2 and weekly_dd >= weekly_limit:  # Wednesday onward
                s.sizes_halved = True

    def size_multiplier(self) -> float:
        return 0.5 if (self.state and self.state.sizes_halved) else 1.0

    def check_capacity(self, open_positions: list, deployed: float, equity: float,
                       new_cost: float, new_symbol: str) -> None:
        p = self.profile
        if self.state and self.state.halted_today:
            raise RiskRejection("Trading halted for the day by drawdown circuit breaker.")
        if len(open_positions) >= p["max_concurrent_positions"]:
            raise RiskRejection(
                f"Max concurrent positions ({p['max_concurrent_positions']}) reached.")
        if (deployed + new_cost) / equity > p["max_portfolio_deployed_pct"] + 1e-9:
            raise RiskRejection(
                f"Trade would deploy {(deployed + new_cost) / equity:.0%} of portfolio "
                f"(limit {p['max_portfolio_deployed_pct']:.0%}).")
        sector = SECTOR_MAP.get(new_symbol, new_symbol)
        same_sector = sum(1 for pos in open_positions
                          if SECTOR_MAP.get(pos["symbol"], pos["symbol"]) == sector)
        if same_sector >= p["max_correlated_positions"]:
            raise RiskRejection(
                f"Already holding {same_sector} positions in '{sector}' "
                f"(limit {p['max_correlated_positions']}).")
