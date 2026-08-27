from __future__ import annotations

"""BTTS V2.9.2 conversion-reliability patch.

Adds two layers on top of V2.9.1:

1) Conversion Reliability Score (CRS): both scoring legs must be independently
   reliable after blending venue-role and all-venue recent evidence.
2) One-sided conversion risk: when one attack is materially more reliable than
   the other, the BTTS consensus is penalized by 5-10 percentage points before
   Premium publication/ranking.

The implementation deliberately uses only pre-kickoff information already
available to the walk-forward engine. It does not try to predict penalties,
red cards or other match events.
"""

from math import exp

from .btts_v25_policy import anti_zero_metrics
from .btts_v291_policy import anti_zero_decision_v291, tier_a_decision_v291

V292_MIN_CRS = 0.60
V292_A_MIN_CRS = 0.62

V292_ONESIDED_GAP_TRIGGER = 0.12
V292_ONESIDED_WEAK_CRS = 0.68
V292_MIN_ONESIDED_PENALTY = 0.05
V292_MAX_ONESIDED_PENALTY = 0.10

V292_MIN_ADJUSTED_CONSENSUS = 0.57
V292_A_MIN_ADJUSTED_CONSENSUS = 0.61


def _bounded(value: float) -> float:
    return max(0.0, min(float(value), 1.0))


def _gf_probability(avg_gf: float) -> float:
    return 1.0 - exp(-max(0.0, float(avg_gf or 0.0)))


def conversion_reliability_score(role_profile: dict, overall_profile: dict) -> float:
    """Pre-kickoff finishing/reliability proxy on a 0-1 scale.

    Historical xG/shot-quality data is not guaranteed for every fixture in the
    current dataset, so V2.9.2 uses robust goal-production evidence rather than
    fabricating unavailable features.
    """
    role_score_prob = _bounded(role_profile.get("score_probability", 0.0))
    overall_score_prob = _bounded(overall_profile.get("score_probability", 0.0))
    role_robust = _gf_probability(role_profile.get("robust_avg_gf", role_profile.get("avg_gf", 0.0)))
    overall_robust = _gf_probability(
        overall_profile.get("robust_avg_gf", overall_profile.get("avg_gf", 0.0))
    )
    role_score_rate = _bounded(role_profile.get("score_rate", 0.0))
    overall_score_rate = _bounded(overall_profile.get("score_rate", 0.0))
    recent_scoring = _bounded(float(overall_profile.get("last5_scored", 0) or 0) / 5.0)

    crs = (
        0.30 * role_score_prob
        + 0.20 * overall_score_prob
        + 0.15 * role_robust
        + 0.10 * overall_robust
        + 0.10 * role_score_rate
        + 0.10 * overall_score_rate
        + 0.05 * recent_scoring
    )
    return round(_bounded(crs), 6)


def v292_conversion_metrics(prediction) -> dict:
    m = anti_zero_metrics(prediction)
    if not m.get("available"):
        return {"available": False}

    home_crs = conversion_reliability_score(m["home"], m["home_overall"])
    away_crs = conversion_reliability_score(m["away"], m["away_overall"])
    weakest_crs = min(home_crs, away_crs)
    strongest_crs = max(home_crs, away_crs)
    crs_gap = strongest_crs - weakest_crs

    penalty = 0.0
    if crs_gap >= V292_ONESIDED_GAP_TRIGGER and weakest_crs < V292_ONESIDED_WEAK_CRS:
        gap_excess = crs_gap - V292_ONESIDED_GAP_TRIGGER
        weak_deficit = V292_ONESIDED_WEAK_CRS - weakest_crs
        penalty = (
            V292_MIN_ONESIDED_PENALTY
            + 0.25 * gap_excess
            + 0.20 * weak_deficit
        )
        penalty = max(V292_MIN_ONESIDED_PENALTY, min(penalty, V292_MAX_ONESIDED_PENALTY))

    adjusted_consensus = max(0.0, float(m.get("consensus_probability", 0.0) or 0.0) - penalty)
    adjusted_calibrated = max(0.0, float(m.get("calibrated_probability", 0.0) or 0.0) - penalty)

    return {
        "available": True,
        "base": m,
        "home_crs": home_crs,
        "away_crs": away_crs,
        "weakest_crs": weakest_crs,
        "strongest_crs": strongest_crs,
        "crs_gap": crs_gap,
        "one_sided_penalty": penalty,
        "adjusted_consensus_probability": adjusted_consensus,
        "adjusted_calibrated_probability": adjusted_calibrated,
    }


def _v292_decision(prediction, *, tier_a: bool = False):
    from .premium_risk_guard import PremiumRiskDecision

    prior = tier_a_decision_v291(prediction) if tier_a else anti_zero_decision_v291(prediction)
    if prior is not None:
        return prior

    m = v292_conversion_metrics(prediction)
    if not m.get("available"):
        return PremiumRiskDecision(True, "v292_evidence_missing", "V2.9.2 conversion evidence unavailable")

    prefix = "v292_a" if tier_a else "v292"
    crs_floor = V292_A_MIN_CRS if tier_a else V292_MIN_CRS
    consensus_floor = V292_A_MIN_ADJUSTED_CONSENSUS if tier_a else V292_MIN_ADJUSTED_CONSENSUS

    if m["home_crs"] < crs_floor:
        return PremiumRiskDecision(
            True,
            f"home_{prefix}_conversion_reliability",
            f"home CRS={m['home_crs']:.1%}<{crs_floor:.0%}",
        )
    if m["away_crs"] < crs_floor:
        return PremiumRiskDecision(
            True,
            f"away_{prefix}_conversion_reliability",
            f"away CRS={m['away_crs']:.1%}<{crs_floor:.0%}",
        )

    if m["one_sided_penalty"] > 0.0 and m["adjusted_consensus_probability"] < consensus_floor:
        return PremiumRiskDecision(
            True,
            f"{prefix}_one_sided_conversion_risk",
            "one-sided conversion penalty="
            f"{m['one_sided_penalty']:.1%}; adjusted consensus="
            f"{m['adjusted_consensus_probability']:.1%}<{consensus_floor:.0%}",
        )

    return None


def anti_zero_decision_v292(prediction):
    return _v292_decision(prediction, tier_a=False)


def tier_a_decision_v292(prediction):
    return _v292_decision(prediction, tier_a=True)


def premium_one_safe_v292(prediction) -> bool:
    return tier_a_decision_v292(prediction) is None


def premium_safety_score_v292(prediction) -> float:
    """Keep V2.9.1 ordering but subtract the explicit one-sided penalty."""
    m = v292_conversion_metrics(prediction)
    if not m.get("available"):
        return 0.0
    base_score = float(m["base"].get("safety_score", 0.0) or 0.0)
    weakest_crs = float(m["weakest_crs"])
    reliability_adjustment = max(-8.0, min(4.0, (weakest_crs - V292_A_MIN_CRS) * 40.0))
    penalty_points = float(m["one_sided_penalty"]) * 100.0
    return round(max(0.0, min(100.0, base_score + reliability_adjustment - penalty_points)), 2)


def install_btts_v292_policy() -> None:
    """Install V2.9.2 through the production policy hooks."""
    from . import btts_v25_policy
    from .premium_risk_guard import PremiumRiskGuard

    if getattr(PremiumRiskGuard, "_btts_v292_installed", False):
        return

    btts_v25_policy.anti_zero_decision = anti_zero_decision_v292
    btts_v25_policy.tier_a_decision = tier_a_decision_v292
    btts_v25_policy.premium_one_safe = premium_one_safe_v292
    btts_v25_policy.premium_safety_score = premium_safety_score_v292
    PremiumRiskGuard._btts_v292_installed = True
