"""
rationale.py — Plain-English trade rationale (spec constraint #6 and §5.6.5).

Deterministic template generation from the actual signals/criteria, so it costs
$0 and never hallucinates a reason that didn't fire. An optional Anthropic API
hook can rewrite the same facts more fluently; off by default to honor the $0
target (enable via trading.rationale_llm_enabled + ANTHROPIC_API_KEY).
"""

from __future__ import annotations

from typing import Optional

CRITERIA_LABELS = {
    "full_signal_alignment": "Full signal alignment: technical, options flow, macro, sentiment agree",
    "institutional_options_flow": "Institutional flow: >$500K unusual premium in trade direction",
    "favorable_risk_reward": "Risk/reward ≥ 3:1 with defined max loss",
    "catalyst_present": "Catalyst present",
    "favorable_iv": "IV rank favorable for this strategy type",
    "benign_market_regime": "Benign market regime (VIX < 35, Fear & Greed > 15)",
    "strong_historical_analog": "Historical analogs ≥ 70% win rate on ≥ 20 samples",
}


def normal_rationale(cand, sized: float, profile: dict, regime: str,
                     guards_halved: bool = False) -> str:
    lines = [
        f"{cand.strategy.replace('_', ' ').title()} on {cand.symbol} "
        f"({cand.direction}, {cand.dte} DTE, IV rank {cand.iv_rank:.0f}).",
        "",
        "Signals that fired:",
    ]
    for s in sorted(cand.signals, key=lambda x: -x.strength):
        lines.append(f"  • [{s.category}] {s.name} ({s.direction}, {s.strength:.2f}): {s.detail}")
    lines += [
        "",
        f"Composite signal strength: {cand.signal_strength:.2f} → signal scalar "
        f"{0.40 + 0.60 * cand.signal_strength:.2f}.",
        f"Risk profile '{profile['name']}': max {profile['max_position_pct']:.0%} per trade, "
        f"abs cap ${profile['max_position_abs_cap']:,}.",
        f"Sized at ${sized:,.2f}"
        + (" (halved by weekly-loss guard)" if guards_halved else "")
        + f". Market regime: {regime}.",
        f"Defined risk: max loss ${cand.max_loss:,.0f}/contract, "
        f"max profit ${cand.max_profit:,.0f}/contract.",
    ]
    if cand.catalyst:
        lines.append(f"Catalyst: {cand.catalyst}.")
    return "\n".join(lines)


def override_rationale(cand, override, sized: float, auto: bool) -> str:
    bolt = {"STRONG": "⚡", "MAX": "⚡⚡", "ALL_IN": "⚡⚡⚡"}[override.tier]
    mult = override.tier_spec.get("size_multiplier")
    head = f"{bolt} HIGH CONVICTION — {override.tier} OVERRIDE"
    if mult:
        head += f" ({mult}×)"
    lines = [
        head, "",
        f"Trade: {cand.symbol} {cand.strategy.replace('_', ' ')}, {cand.dte} DTE",
        f"Normal size: ${override.normal_size:,.0f} → Override size: ${sized:,.0f}",
        "",
        "Why this trade was flagged as exceptional:",
    ]
    for key, met in override.criteria.items():
        lines.append(f"  {'✅' if met else '❌'} {CRITERIA_LABELS[key]}")
    lines += [
        "",
        f"Criteria met: {override.criteria_met}/7 → qualifies for {override.tier} tier",
        ("Override approved: auto-execute" if auto
         else "Override approved: user-confirmed via dashboard modal"),
    ]
    return "\n".join(lines)


def llm_polish(text: str, enabled: bool = False) -> str:
    """Optional fluent rewrite via the Anthropic API. Same facts in, prose out."""
    if not enabled:
        return text
    try:
        import os
        import anthropic  # type: ignore
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=400,
            messages=[{"role": "user", "content":
                       "Rewrite this trade rationale as 3-4 clear sentences for a "
                       "trade log. Keep every number. Do not add claims:\n\n" + text}])
        return msg.content[0].text
    except Exception:
        return text


def overnight_summary_sentence(bias: str, etf_moves: dict, fired: list) -> str:
    movers = ", ".join(f"{k} {v:+.1f}%" for k, v in
                       sorted(etf_moves.items(), key=lambda kv: abs(kv[1]), reverse=True)[:3])
    return (f"Pre-market bias: {bias.upper()}. Overnight movers: {movers}. "
            f"{len(fired)} overlay rule(s) active.")
