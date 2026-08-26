from __future__ import annotations

"""BTTS V2.9.1 precision patch.

Adds conservative gates learned from the Universitario de Vinto 2-0 Nacional
Potosi audit without trying to predict red cards.  The objective is to reject
BTTS candidates whose weakest scoring leg is too fragile, whose recent goal
production is outlier-dependent, or whose calibrated probability materially
disagrees with the bilateral consensus signal.
"""

from .btts_v25_policy import anti_zero_metrics
from .btts_v27_policy import anti_zero_decision_v27, tier_a_decision_v27

# V2.9.1: weakest leg must be independently strong.
V291_MIN_WEAKEST_SCORE_PROB = 0.70
V291_A_MIN_WEAKEST_SCORE_PROB = 0.73

# Conversion/robustness proxy: remove the highest-scoring match before judging
# the attack.  This prevents one blowout from carrying a Premium candidate.
V291_MIN_ROBUST_AVG_GF = 0.90
V291_A_MIN_ROBUST_AVG_GF = 1.00
V291_MAX_OUTLIER_AVG_DROP = 0.25
V291_A_MAX_OUTLIER_AVG_DROP = 0.20

# Model-consensus disagreement.  Large gaps mean the headline probability is
# not sufficiently confirmed by the two independent scoring legs.
V291_MAX_MODEL_CONSENSUS_GAP = 0.07
V291_A_MAX_MODEL_CONSENSUS_GAP = 0.05


def _v291_decision(prediction, *, tier_a: bool = False):
    from .premium_risk_guard import PremiumRiskDecision

    m = anti_zero_metrics(prediction)
    if not m.get("available"):
        return PremiumRiskDecision(True, "v291_evidence_missing", "V2.9.1 evidence unavailable")

    prefix = "v291_a" if tier_a else "v291"
    weakest_floor = V291_A_MIN_WEAKEST_SCORE_PROB if tier_a else V291_MIN_WEAKEST_SCORE_PROB
    robust_floor = V291_A_MIN_ROBUST_AVG_GF if tier_a else V291_MIN_ROBUST_AVG_GF
    max_outlier_drop = V291_A_MAX_OUTLIER_AVG_DROP if tier_a else V291_MAX_OUTLIER_AVG_DROP
    max_gap = V291_A_MAX_MODEL_CONSENSUS_GAP if tier_a else V291_MAX_MODEL_CONSENSUS_GAP

    if m["weakest_score_probability"] < weakest_floor:
        return PremiumRiskDecision(
            True,
            f"{prefix}_weakest_probability",
            f"weakest scoring probability={m['weakest_score_probability']:.1%}<{weakest_floor:.0%}",
        )

    # Conversion/finishing robustness for both scoring legs.
    for side in ("home", "away"):
        p = m[side]
        robust_avg = float(p.get("robust_avg_gf", p.get("avg_gf", 0.0)) or 0.0)
        outlier_drop = float(p.get("outlier_avg_drop", 0.0) or 0.0)
        if robust_avg < robust_floor:
            return PremiumRiskDecision(
                True,
                f"{side}_{prefix}_conversion_floor",
                f"{side} robust avg GF={robust_avg:.2f}<{robust_floor:.2f}",
            )
        if outlier_drop > max_outlier_drop:
            return PremiumRiskDecision(
                True,
                f"{side}_{prefix}_outlier_conversion",
                f"{side} attack depends too much on an outlier match ({outlier_drop:.1%}>{max_outlier_drop:.0%})",
            )

    calibrated = float(m.get("calibrated_probability", 0.0) or 0.0)
    consensus = float(m.get("consensus_probability", 0.0) or 0.0)
    gap = abs(calibrated - consensus)
    if gap > max_gap:
        return PremiumRiskDecision(
            True,
            f"{prefix}_model_disagreement",
            f"calibrated/consensus gap={gap:.1%}>{max_gap:.0%}",
        )

    # Keep every V2.9 bilateral/opponent-concession/anti-zero gate underneath.
    return tier_a_decision_v27(prediction) if tier_a else anti_zero_decision_v27(prediction)


def anti_zero_decision_v291(prediction):
    return _v291_decision(prediction, tier_a=False)


def tier_a_decision_v291(prediction):
    return _v291_decision(prediction, tier_a=True)


def premium_one_safe_v291(prediction) -> bool:
    return tier_a_decision_v291(prediction) is None


def install_btts_v291_policy() -> None:
    """Install V2.9.1 after V2.9 through the production policy hooks."""
    from . import btts_v25_policy
    from .premium_risk_guard import PremiumRiskGuard

    if getattr(PremiumRiskGuard, "_btts_v291_installed", False):
        return
    btts_v25_policy.anti_zero_decision = anti_zero_decision_v291
    btts_v25_policy.tier_a_decision = tier_a_decision_v291
    btts_v25_policy.premium_one_safe = premium_one_safe_v291
    PremiumRiskGuard._btts_v291_installed = True
