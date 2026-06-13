"""
overrides.py — High-conviction override system (spec §5.6).

Flow: signal ≥ 0.90 → evaluate 7 criteria → determine tier → gate by user config
→ STRONG/MAX auto-execute (if enabled) / ALL_IN waits for user approval with a
timeout fallback to normal sizing.

The universal $10k cap is applied in risk.compute_override_size — no tier escapes it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Optional

OVERRIDE_TIERS = {
    "STRONG": {
        "size_multiplier": 1.5,
        "abs_cap_multiplier": 1.5,
        "max_overrides_per_day": 3,
        "min_signal_strength": 0.90,
        "min_criteria_met": 5,
        "user_notification": "toast",
    },
    "MAX": {
        "size_multiplier": 2.0,
        "abs_cap_multiplier": 2.0,
        "max_overrides_per_day": 1,
        "min_signal_strength": 0.95,
        "min_criteria_met": 6,
        "user_notification": "modal",
    },
    "ALL_IN": {
        "size_multiplier": None,
        "fixed_pct_of_cash": 0.50,
        # Spec lists $20k here, but the universal $10k cap "cannot be overridden"
        # (constraint #5), so $10k is the effective ceiling. Kept at the spec value
        # to show intent; risk.compute_override_size clamps to $10k regardless.
        "abs_hard_cap": 20_000,
        "max_overrides_per_day": 1,
        "max_overrides_per_week": 1,
        "min_signal_strength": 0.98,
        "min_criteria_met": 7,
        "user_notification": "modal",
        "user_approval_required": True,
    },
}

TIER_ORDER = ["STRONG", "MAX", "ALL_IN"]


def evaluate_conviction_criteria(trade_signal, market_context) -> tuple[dict, int]:
    """The 7 binary conviction criteria (spec §5.6.2).

    trade_signal: strategies.TradeCandidate-like object.
    market_context: object with .vix and .fear_greed_index.
    """
    c = {}
    c["full_signal_alignment"] = (
        trade_signal.technical_direction
        == trade_signal.options_flow_direction
        == trade_signal.macro_direction
        == trade_signal.sentiment_direction
        and trade_signal.technical_direction in ("bullish", "bearish")
    )
    c["institutional_options_flow"] = (
        (trade_signal.unusual_options_premium or 0) > 500_000
        and trade_signal.options_flow_direction == trade_signal.technical_direction
    )
    c["favorable_risk_reward"] = (
        trade_signal.max_loss > 0 and trade_signal.max_profit / trade_signal.max_loss >= 3.0
    )
    c["catalyst_present"] = trade_signal.catalyst is not None
    if trade_signal.strategy_type in ("long_options", "spread", "0dte"):
        c["favorable_iv"] = trade_signal.iv_rank < 35
    else:
        c["favorable_iv"] = trade_signal.iv_rank > 55
    c["benign_market_regime"] = (
        market_context.vix < 35 and market_context.fear_greed_index > 15
    )
    c["strong_historical_analog"] = (
        (trade_signal.backtest_win_rate or 0) >= 0.70
        and (trade_signal.backtest_sample_size or 0) >= 20
    )
    return c, sum(c.values())


def determine_tier(signal_strength: float, criteria_met: int) -> Optional[str]:
    if criteria_met >= 7 and signal_strength >= 0.98:
        return "ALL_IN"
    if criteria_met >= 6 and signal_strength >= 0.95:
        return "MAX"
    if criteria_met >= 5 and signal_strength >= 0.90:
        return "STRONG"
    return None


@dataclass
class ApprovedOverride:
    tier: str
    criteria: dict
    criteria_met: int
    normal_size: float
    requires_approval: bool = False
    approval_id: Optional[str] = None
    expires_at: Optional[dt.datetime] = None
    approved: Optional[bool] = None  # None = pending
    _symbol: Optional[str] = None    # for de-duping pending modals per symbol

    @property
    def tier_spec(self) -> dict:
        return OVERRIDE_TIERS[self.tier]

    def is_valid(self) -> bool:
        if not self.requires_approval:
            return True
        if self.approved is True:
            return True
        return False  # pending or skipped → caller falls back to normal sizing


@dataclass
class OverrideManager:
    """Stateful gatekeeper: config gating, tier downgrades, per-day/week limits,
    and the ALL_IN approval queue."""
    config: dict  # the "conviction_overrides" block from config.json
    executed_today: dict = field(default_factory=lambda: {t: 0 for t in TIER_ORDER})
    executed_this_week: int = 0
    total_today: int = 0
    rejected_log: list = field(default_factory=list)
    pending: dict = field(default_factory=dict)  # approval_id -> ApprovedOverride

    def reset_day(self):
        self.executed_today = {t: 0 for t in TIER_ORDER}
        self.total_today = 0

    def reset_week(self):
        self.executed_this_week = 0

    def evaluate(self, trade_signal, market_context, normal_size: float,
                 now: Optional[dt.datetime] = None) -> Optional[ApprovedOverride]:
        """Returns an ApprovedOverride (possibly pending approval) or None
        (use normal sizing). Every blocked attempt is logged if configured."""
        now = now or dt.datetime.now()
        if trade_signal.signal_strength < 0.90:
            return None

        criteria, met = evaluate_conviction_criteria(trade_signal, market_context)
        tier = determine_tier(trade_signal.signal_strength, met)
        if tier is None:
            return None

        # Don't stack a second approval modal for a setup already awaiting the
        # user — the next scan re-qualifies the same symbol and would otherwise
        # queue a fresh approval_id every cycle.
        symbol = getattr(trade_signal, "symbol", None)
        if any(p.requires_approval and p.approved is None
               and getattr(p, "_symbol", None) == symbol
               for p in self.pending.values()):
            return None

        if not self.config.get("enabled", False):
            self._log_rejection(tier, met, "overrides disabled in config", trade_signal)
            return None

        # Downgrade above the user's allowed ceiling.
        max_tier = self.config.get("max_allowed_tier", "MAX")
        if TIER_ORDER.index(tier) > TIER_ORDER.index(max_tier):
            tier = max_tier

        # Rate limits: per-tier, global daily, global weekly.
        spec = OVERRIDE_TIERS[tier]
        if self.executed_today[tier] >= spec["max_overrides_per_day"]:
            self._log_rejection(tier, met, f"{tier} daily tier limit reached", trade_signal)
            return None
        if tier == "ALL_IN" and self.executed_this_week >= spec.get("max_overrides_per_week", 1):
            self._log_rejection(tier, met, "ALL_IN weekly limit reached", trade_signal)
            return None
        if self.total_today >= self.config.get("max_override_trades_per_day", 2):
            self._log_rejection(tier, met, "global daily override limit reached", trade_signal)
            return None

        ov = ApprovedOverride(tier=tier, criteria=criteria, criteria_met=met,
                              normal_size=normal_size, _symbol=symbol)

        needs_approval = (
            tier == "ALL_IN" and self.config.get("all_in_requires_approval", True)
        ) or (
            tier == "STRONG" and not self.config.get("auto_execute_strong", True)
        ) or (
            tier == "MAX" and not self.config.get("auto_execute_max", True)
        )
        if needs_approval:
            ov.requires_approval = True
            ov.approval_id = uuid.uuid4().hex[:12]
            ov.expires_at = now + dt.timedelta(
                seconds=self.config.get("approval_timeout_seconds", 300))
            self.pending[ov.approval_id] = ov
        return ov

    def record_execution(self, ov: ApprovedOverride):
        self.executed_today[ov.tier] += 1
        self.total_today += 1
        if ov.tier == "ALL_IN":
            self.executed_this_week += 1

    def resolve(self, approval_id: str, approved: bool) -> Optional[ApprovedOverride]:
        ov = self.pending.pop(approval_id, None)
        if ov:
            ov.approved = approved
        return ov

    def expire_stale(self, now: Optional[dt.datetime] = None) -> list:
        """Returns overrides whose 5-min window lapsed; caller executes normal size."""
        now = now or dt.datetime.now()
        expired = [k for k, v in self.pending.items() if v.expires_at and v.expires_at <= now]
        out = []
        for k in expired:
            ov = self.pending.pop(k)
            ov.approved = False
            out.append(ov)
        return out

    def _log_rejection(self, tier, met, reason, trade_signal):
        if self.config.get("log_rejected_overrides", True):
            self.rejected_log.append({
                "ts": dt.datetime.now().isoformat(),
                "tier": tier, "criteria_met": met, "reason": reason,
                "symbol": getattr(trade_signal, "symbol", "?"),
                "signal_strength": trade_signal.signal_strength,
            })
