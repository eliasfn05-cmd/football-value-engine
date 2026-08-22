from __future__ import annotations

"""BTTS V2.7 anti-0-0 precision layer.

V2.7 is a targeted refinement after repeated 0-x/x-0 failures. It keeps V2.6
recall rebalancing, but makes the weakest attack authoritative for Premium
publication and makes Premium A materially harder than Premium B.

Key principles:
* BTTS is a two-team event: the weakest attack sets the ceiling.
* A strong H2H/aggregate BTTS history cannot rescue a side that often blanks.
* Explicit zero-goal risk is a hard veto when it is excessive.
* Premium A requires stronger bilateral scoring evidence than the generic gate.
"""

from .btts_v25_policy import anti_zero_metrics

# Generic Premium floors. These are deliberately stricter on the weakest side
# than V2.6, while avoiding the old duplicate BTTS-rate veto stacking.
V27_MIN_ROLE_SCORE_RATE = 0.60
V27_MIN_ROLE_AVG_GF = 0.95
V27_MIN_ROLE_LAST5_SCORED = 3
V27_MAX_ROLE_ZERO_RISK = 0.35
V27_MIN_OVERALL_SCORE_RATE = 0.58
V27_MIN_OVERALL_LAST5_SCORED = 3
V27_MIN_WEAKEST_SCORE_PROB = 0.66
V27_MIN_CALIBRATED_PROB = 0.57
V27_MIN_CONSENSUS_PROB = 0.54

# Premium A / #1 floors: both attacks must independently look robust.
V27_A_MIN_ROLE_SCORE_RATE = 0.70
V27_A_MIN_ROLE_AVG_GF = 1.10
V27_A_MIN_ROLE_LAST5_SCORED = 4
V27_A_MAX_ROLE_ZERO_RISK = 0.27
V27_A_MIN_OVERALL_SCORE_RATE = 0.65
V27_A_MIN_OVERALL_LAST5_SCORED = 4
V27_A_MIN_WEAKEST_SCORE_PROB = 0.71
V27_A_MIN_CALIBRATED_PROB = 0.61
V27_A_MIN_CONSENSUS_PROB = 0.58


def _decision(prediction, *, tier_a: bool = False):
    from .premium_risk_guard import PremiumRiskDecision

    m = anti_zero_metrics(prediction)
    if not m.get("available"):
        # Missing enrichment remains neutral for generic Premium so V2.7 does
        # not recreate the V2.5 zero-pick collapse. Premium A is different:
        # the top pick must have bilateral evidence available.
        if tier_a:
            return PremiumRiskDecision(True, "v27_a_evidence_missing", "anti-zero evidence unavailable")
        return None

    role_score = V27_A_MIN_ROLE_SCORE_RATE if tier_a else V27_MIN_ROLE_SCORE_RATE
    role_avg = V27_A_MIN_ROLE_AVG_GF if tier_a else V27_MIN_ROLE_AVG_GF
    role_last5 = V27_A_MIN_ROLE_LAST5_SCORED if tier_a else V27_MIN_ROLE_LAST5_SCORED
    max_zero = V27_A_MAX_ROLE_ZERO_RISK if tier_a else V27_MAX_ROLE_ZERO_RISK
    overall_score = V27_A_MIN_OVERALL_SCORE_RATE if tier_a else V27_MIN_OVERALL_SCORE_RATE
    overall_last5 = V27_A_MIN_OVERALL_LAST5_SCORED if tier_a else V27_MIN_OVERALL_LAST5_SCORED
    weakest_floor = V27_A_MIN_WEAKEST_SCORE_PROB if tier_a else V27_MIN_WEAKEST_SCORE_PROB
    calibrated_floor = V27_A_MIN_CALIBRATED_PROB if tier_a else V27_MIN_CALIBRATED_PROB
    consensus_floor = V27_A_MIN_CONSENSUS_PROB if tier_a else V27_MIN_CONSENSUS_PROB
    prefix = "v27_a" if tier_a else "v27"

    for side in ("home", "away"):
        p = m[side]
        if p["score_rate"] < role_score:
            return PremiumRiskDecision(True, f"{side}_{prefix}_low_score_rate", f"{side} scores {p['score_rate']:.0%}<{role_score:.0%}")
        if p["avg_gf"] < role_avg:
            return PremiumRiskDecision(True, f"{side}_{prefix}_low_avg_gf", f"{side} avgGF={p['avg_gf']:.2f}<{role_avg:.2f}")
        if p["last5_scored"] < role_last5:
            return PremiumRiskDecision(True, f"{side}_{prefix}_recent_blanks", f"{side} scored {p['last5_scored']}/5<{role_last5}")
        if p["zero_risk"] > max_zero:
            return PremiumRiskDecision(True, f"{side}_{prefix}_zero_risk", f"{side} P(0)={p['zero_risk']:.1%}>{max_zero:.0%}")

        overall = m[f"{side}_overall"]
        if overall["score_rate"] < overall_score:
            return PremiumRiskDecision(True, f"{side}_{prefix}_overall_score", f"{side} overall scores {overall['score_rate']:.0%}<{overall_score:.0%}")
        if overall["last5_scored"] < overall_last5:
            return PremiumRiskDecision(True, f"{side}_{prefix}_overall_last5", f"{side} overall scored {overall['last5_scored']}/5<{overall_last5}")

    # Weakest-Attack Gate: one prolific side cannot compensate for a fragile one.
    if m["weakest_score_probability"] < weakest_floor:
        return PremiumRiskDecision(
            True,
            f"{prefix}_weakest_attack",
            f"weakest scoring probability={m['weakest_score_probability']:.1%}<{weakest_floor:.0%}",
        )
    if m["max_zero_risk"] > max_zero:
        return PremiumRiskDecision(True, f"{prefix}_max_zero_risk", f"max P(0)={m['max_zero_risk']:.1%}>{max_zero:.0%}")
    if m["calibrated_probability"] < calibrated_floor:
        return PremiumRiskDecision(True, f"{prefix}_calibrated_floor", f"calibrated BTTS={m['calibrated_probability']:.1%}<{calibrated_floor:.0%}")
    if m["consensus_probability"] < consensus_floor:
        return PremiumRiskDecision(True, f"{prefix}_consensus_floor", f"bilateral consensus={m['consensus_probability']:.1%}<{consensus_floor:.0%}")
    return None


def anti_zero_decision_v27(prediction):
    return _decision(prediction, tier_a=False)


def tier_a_decision_v27(prediction):
    return _decision(prediction, tier_a=True)


def premium_one_safe_v27(prediction) -> bool:
    """Top Premium is allowed only if it clears the stricter V2.7 A gate."""
    return tier_a_decision_v27(prediction) is None


def install_btts_v27_policy() -> None:
    """Install V2.7 after V2.6 so all V2.3 publication paths use it."""
    from . import btts_v25_policy
    from .premium_risk_guard import PremiumRiskGuard

    if getattr(PremiumRiskGuard, "_btts_v27_installed", False):
        return

    # V2.3 imports these functions dynamically at publication time, so replacing
    # them here updates selector, replacement and dashboard final gates together.
    btts_v25_policy.anti_zero_decision = anti_zero_decision_v27
    btts_v25_policy.tier_a_decision = tier_a_decision_v27
    btts_v25_policy.premium_one_safe = premium_one_safe_v27

    PremiumRiskGuard._btts_v27_installed = True
