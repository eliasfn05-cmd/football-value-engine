from __future__ import annotations

"""BTTS V2.9.11 hard two-goal environment guard.

Post-mortem target: Malmo - AIK 0-0 (2026-09-07), immediately after the
Jong Utrecht - FC Eindhoven 0-0 audit.

BTTS necessarily needs at least two total goals. V2.9.11 therefore refuses to
publish a BTTS candidate unless the pre-kickoff scoring/concession environment
also supports a strong independent Over 1.5-style goal-volume floor.

This does not make a 0-0 impossible. It deliberately trades selection volume
for precision and converts marginal low-tempo profiles into NO BET.
"""

from math import exp

from .btts_v25_policy import anti_zero_metrics
from .btts_v27_policy import _opponent_concession_metrics
from .btts_v2910_policy import anti_zero_decision_v2910, tier_a_decision_v2910

# Generic published candidate.
V2911_MIN_TOTAL_LAMBDA = 2.45
V2911_MIN_P_GE_2_GOALS = 0.70
V2911_MAX_P_ZERO_ZERO = 0.09
V2911_MIN_WEAKEST_GOAL_LAMBDA = 0.95
V2911_MIN_ROLE_MATCH_GOALS = 2.35

# Tier A / Top-3: only high-goal environments survive.
V2911_A_MIN_TOTAL_LAMBDA = 2.80
V2911_A_MIN_P_GE_2_GOALS = 0.76
V2911_A_MAX_P_ZERO_ZERO = 0.065
V2911_A_MIN_WEAKEST_GOAL_LAMBDA = 1.10
V2911_A_MIN_ROLE_MATCH_GOALS = 2.60


def _bounded_non_negative(value) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def v2911_goal_environment_metrics(prediction) -> dict:
    """Independent pre-kickoff total-goal confirmation for BTTS publication.

    Expected scoring legs blend each team's robust role attack with the exact
    opponent defensive-role goals conceded. We then calculate a Poisson proxy
    for P(total goals >= 2) and P(0-0). No market odds are required.
    """
    m = anti_zero_metrics(prediction)
    c = _opponent_concession_metrics(prediction)
    if not m.get("available") or not c.get("available"):
        return {"available": False}

    home_attack = _bounded_non_negative(m["home"].get("robust_avg_gf", m["home"].get("avg_gf", 0.0)))
    away_attack = _bounded_non_negative(m["away"].get("robust_avg_gf", m["away"].get("avg_gf", 0.0)))
    away_def_ga = _bounded_non_negative(c["home_scoring_vs"].get("avg_ga", 0.0))
    home_def_ga = _bounded_non_negative(c["away_scoring_vs"].get("avg_ga", 0.0))

    # Conservative blend: an attack does not get full credit unless the
    # opponent's corresponding defensive role also concedes goals.
    home_lambda = 0.55 * home_attack + 0.45 * away_def_ga
    away_lambda = 0.55 * away_attack + 0.45 * home_def_ga
    total_lambda = home_lambda + away_lambda

    p_zero_zero = exp(-total_lambda)
    p_ge_2 = 1.0 - exp(-total_lambda) * (1.0 + total_lambda)

    # Match-goal environment from both exact venue role histories. This catches
    # low-tempo teams that may still have acceptable scoring percentages.
    home_role_match_goals = _bounded_non_negative(m["home"].get("avg_gf")) + _bounded_non_negative(m["home"].get("avg_ga"))
    away_role_match_goals = _bounded_non_negative(m["away"].get("avg_gf")) + _bounded_non_negative(m["away"].get("avg_ga"))
    weakest_role_match_goals = min(home_role_match_goals, away_role_match_goals)

    return {
        "available": True,
        "home_goal_lambda": home_lambda,
        "away_goal_lambda": away_lambda,
        "weakest_goal_lambda": min(home_lambda, away_lambda),
        "total_goal_lambda": total_lambda,
        "p_ge_2_goals": p_ge_2,
        "p_zero_zero": p_zero_zero,
        "home_role_match_goals": home_role_match_goals,
        "away_role_match_goals": away_role_match_goals,
        "weakest_role_match_goals": weakest_role_match_goals,
    }


def _decision(prediction, *, tier_a: bool = False):
    from .premium_risk_guard import PremiumRiskDecision

    prior = tier_a_decision_v2910(prediction) if tier_a else anti_zero_decision_v2910(prediction)
    if prior is not None:
        return prior

    g = v2911_goal_environment_metrics(prediction)
    if not g.get("available"):
        return PremiumRiskDecision(True, "v2911_goal_environment_missing", "V2.9.11 goal-volume evidence unavailable")

    prefix = "v2911_a" if tier_a else "v2911"
    min_total = V2911_A_MIN_TOTAL_LAMBDA if tier_a else V2911_MIN_TOTAL_LAMBDA
    min_p2 = V2911_A_MIN_P_GE_2_GOALS if tier_a else V2911_MIN_P_GE_2_GOALS
    max_p00 = V2911_A_MAX_P_ZERO_ZERO if tier_a else V2911_MAX_P_ZERO_ZERO
    min_weak = V2911_A_MIN_WEAKEST_GOAL_LAMBDA if tier_a else V2911_MIN_WEAKEST_GOAL_LAMBDA
    min_role_goals = V2911_A_MIN_ROLE_MATCH_GOALS if tier_a else V2911_MIN_ROLE_MATCH_GOALS

    if g["total_goal_lambda"] < min_total:
        return PremiumRiskDecision(True, f"{prefix}_total_goal_lambda", f"total goal lambda={g['total_goal_lambda']:.2f}<{min_total:.2f}")
    if g["p_ge_2_goals"] < min_p2:
        return PremiumRiskDecision(True, f"{prefix}_two_goal_probability", f"P(total>=2)={g['p_ge_2_goals']:.1%}<{min_p2:.0%}")
    if g["p_zero_zero"] > max_p00:
        return PremiumRiskDecision(True, f"{prefix}_zero_zero_probability", f"P(0-0)={g['p_zero_zero']:.1%}>{max_p00:.1%}")
    if g["weakest_goal_lambda"] < min_weak:
        return PremiumRiskDecision(True, f"{prefix}_weakest_goal_lambda", f"weakest goal lambda={g['weakest_goal_lambda']:.2f}<{min_weak:.2f}")
    if g["weakest_role_match_goals"] < min_role_goals:
        return PremiumRiskDecision(True, f"{prefix}_low_tempo_role", f"weakest role match goals={g['weakest_role_match_goals']:.2f}<{min_role_goals:.2f}")

    return None


def anti_zero_decision_v2911(prediction):
    return _decision(prediction, tier_a=False)


def tier_a_decision_v2911(prediction):
    return _decision(prediction, tier_a=True)


def premium_one_safe_v2911(prediction) -> bool:
    return tier_a_decision_v2911(prediction) is None


def install_btts_v2911_policy() -> None:
    """Install V2.9.11 as the final production BTTS precision layer."""
    from . import btts_v25_policy
    from .premium_risk_guard import PremiumRiskGuard

    if getattr(PremiumRiskGuard, "_btts_v2911_installed", False):
        return
    btts_v25_policy.anti_zero_decision = anti_zero_decision_v2911
    btts_v25_policy.tier_a_decision = tier_a_decision_v2911
    btts_v25_policy.premium_one_safe = premium_one_safe_v2911
    PremiumRiskGuard._btts_v2911_installed = True
