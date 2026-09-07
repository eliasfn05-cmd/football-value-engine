from __future__ import annotations

"""BTTS V2.9.13 ultra-low-total hard guard.

Post-mortem target: Estoril - Arouca 0-0 (2026-09-07), following the same-day
Jong Utrecht - Eindhoven 0-0, Malmo - AIK 0-0 and Orebro - Ostersund 0-1 losses.

This layer is intentionally severe. BTTS needs at least two goals, so any
candidate with even modest recent <=1-goal evidence is hidden. The objective is
precision and drawdown control, accepting frequent NO BET days.
"""

from .btts_v2912_policy import (
    anti_zero_decision_v2912,
    tier_a_decision_v2912,
    v2912_low_total_metrics,
)

# Generic publication: near-clean recent 2+ goal history required.
V2913_MIN_TOTAL_LAMBDA = 3.05
V2913_MIN_P_GE_2_GOALS = 0.81
V2913_MIN_WEAKEST_GOAL_LAMBDA = 1.15
V2913_MIN_ROLE_LAST5_GE2 = 5
V2913_MIN_OVERALL_LAST5_GE2 = 5
V2913_MIN_ROLE_AVG_TOTAL = 2.80
V2913_MIN_OVERALL_AVG_TOTAL = 2.70
V2913_MAX_ROLE_LAST5_LOW_TOTAL = 0
V2913_MAX_OVERALL_LAST5_LOW_TOTAL = 0

# Tier A / Top-3: only very high-goal environments survive.
V2913_A_MIN_TOTAL_LAMBDA = 3.35
V2913_A_MIN_P_GE_2_GOALS = 0.85
V2913_A_MIN_WEAKEST_GOAL_LAMBDA = 1.30
V2913_A_MIN_ROLE_LAST5_GE2 = 5
V2913_A_MIN_OVERALL_LAST5_GE2 = 5
V2913_A_MIN_ROLE_AVG_TOTAL = 3.00
V2913_A_MIN_OVERALL_AVG_TOTAL = 2.90
V2913_A_MAX_ROLE_LAST5_LOW_TOTAL = 0
V2913_A_MAX_OVERALL_LAST5_LOW_TOTAL = 0


def _decision(prediction, *, tier_a: bool = False):
    from .premium_risk_guard import PremiumRiskDecision

    prior = tier_a_decision_v2912(prediction) if tier_a else anti_zero_decision_v2912(prediction)
    if prior is not None:
        return prior

    m = v2912_low_total_metrics(prediction)
    if not m.get("available"):
        return PremiumRiskDecision(True, "v2913_evidence_missing", "V2.9.13 low-total evidence unavailable")

    g = m["goal_environment"]
    prefix = "v2913_a" if tier_a else "v2913"
    min_total = V2913_A_MIN_TOTAL_LAMBDA if tier_a else V2913_MIN_TOTAL_LAMBDA
    min_p2 = V2913_A_MIN_P_GE_2_GOALS if tier_a else V2913_MIN_P_GE_2_GOALS
    min_weak = V2913_A_MIN_WEAKEST_GOAL_LAMBDA if tier_a else V2913_MIN_WEAKEST_GOAL_LAMBDA
    min_role5 = V2913_A_MIN_ROLE_LAST5_GE2 if tier_a else V2913_MIN_ROLE_LAST5_GE2
    min_overall5 = V2913_A_MIN_OVERALL_LAST5_GE2 if tier_a else V2913_MIN_OVERALL_LAST5_GE2
    min_role_avg = V2913_A_MIN_ROLE_AVG_TOTAL if tier_a else V2913_MIN_ROLE_AVG_TOTAL
    min_overall_avg = V2913_A_MIN_OVERALL_AVG_TOTAL if tier_a else V2913_MIN_OVERALL_AVG_TOTAL
    max_role_low = V2913_A_MAX_ROLE_LAST5_LOW_TOTAL if tier_a else V2913_MAX_ROLE_LAST5_LOW_TOTAL
    max_overall_low = V2913_A_MAX_OVERALL_LAST5_LOW_TOTAL if tier_a else V2913_MAX_OVERALL_LAST5_LOW_TOTAL

    if float(g.get("total_goal_lambda", 0.0)) < min_total:
        return PremiumRiskDecision(True, f"{prefix}_total_lambda", f"total lambda={g.get('total_goal_lambda', 0.0):.2f}<{min_total:.2f}")
    if float(g.get("p_ge_2_goals", 0.0)) < min_p2:
        return PremiumRiskDecision(True, f"{prefix}_p2_floor", f"P(total>=2)={g.get('p_ge_2_goals', 0.0):.1%}<{min_p2:.0%}")
    if float(g.get("weakest_goal_lambda", 0.0)) < min_weak:
        return PremiumRiskDecision(True, f"{prefix}_weak_goal_lambda", f"weakest goal lambda={g.get('weakest_goal_lambda', 0.0):.2f}<{min_weak:.2f}")
    if int(m.get("min_role_last5_ge2", 0)) < min_role5:
        return PremiumRiskDecision(True, f"{prefix}_role_last5_not_clean", f"weakest role 2+ goals={m.get('min_role_last5_ge2', 0)}/5<{min_role5}/5")
    if int(m.get("min_overall_last5_ge2", 0)) < min_overall5:
        return PremiumRiskDecision(True, f"{prefix}_overall_last5_not_clean", f"weakest overall 2+ goals={m.get('min_overall_last5_ge2', 0)}/5<{min_overall5}/5")
    if int(m.get("max_role_last5_low_total", 5)) > max_role_low:
        return PremiumRiskDecision(True, f"{prefix}_role_low_total_present", f"recent role <=1-goal matches={m.get('max_role_last5_low_total', 5)}")
    if int(m.get("max_overall_last5_low_total", 5)) > max_overall_low:
        return PremiumRiskDecision(True, f"{prefix}_overall_low_total_present", f"recent overall <=1-goal matches={m.get('max_overall_last5_low_total', 5)}")
    if float(m.get("min_role_avg_total", 0.0)) < min_role_avg:
        return PremiumRiskDecision(True, f"{prefix}_role_avg_total", f"weakest role avg total={m.get('min_role_avg_total', 0.0):.2f}<{min_role_avg:.2f}")
    if float(m.get("min_overall_avg_total", 0.0)) < min_overall_avg:
        return PremiumRiskDecision(True, f"{prefix}_overall_avg_total", f"weakest overall avg total={m.get('min_overall_avg_total', 0.0):.2f}<{min_overall_avg:.2f}")

    return None


def anti_zero_decision_v2913(prediction):
    return _decision(prediction, tier_a=False)


def tier_a_decision_v2913(prediction):
    return _decision(prediction, tier_a=True)


def premium_one_safe_v2913(prediction) -> bool:
    return tier_a_decision_v2913(prediction) is None


def install_btts_v2913_policy() -> None:
    from . import btts_v25_policy
    from .premium_risk_guard import PremiumRiskGuard

    if getattr(PremiumRiskGuard, "_btts_v2913_installed", False):
        return
    btts_v25_policy.anti_zero_decision = anti_zero_decision_v2913
    btts_v25_policy.tier_a_decision = tier_a_decision_v2913
    btts_v25_policy.premium_one_safe = premium_one_safe_v2913
    PremiumRiskGuard._btts_v2913_installed = True
