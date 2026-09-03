from __future__ import annotations

"""BTTS V2.9.9 emergency precision guard.

Post-mortem targets: Copenhagen-Nordsjaelland 2-0 and Lugano-Servette 1-0.
Both are ONE_SIDED BTTS losses.  V2.9.9 is deliberately conservative: a
candidate cannot be Tier A merely because both attacks have recently scored;
the weaker scoring leg must also face a defence that has repeatedly conceded.

This patch is additive over V2.9.1.  It does not weaken any existing gate.
"""

from .btts_v25_policy import anti_zero_metrics
from .btts_v27_policy import _opponent_concession_metrics
from .btts_v291_policy import anti_zero_decision_v291, tier_a_decision_v291

# Generic Premium: reject obvious clean-sheet / one-sided traps.
V299_MIN_WEAKEST_SCORE_PROB = 0.72
V299_MIN_WEAKEST_OVERALL_LAST5_SCORED = 4
V299_MAX_WEAKEST_OVERALL_FTS = 0.30
V299_MIN_OPP_LAST5_CONCEDED = 4
V299_MAX_OPP_LAST5_CLEAN_SHEETS = 1

# Tier A / Top-3: require stronger independent proof for BOTH scoring legs.
V299_A_MIN_WEAKEST_SCORE_PROB = 0.75
V299_A_MIN_WEAKEST_OVERALL_LAST5_SCORED = 4
V299_A_MAX_WEAKEST_OVERALL_FTS = 0.25
V299_A_MIN_OPP_LAST5_CONCEDED = 4
V299_A_MAX_OPP_LAST5_CLEAN_SHEETS = 1
V299_A_MIN_CONSENSUS_PROB = 0.63
V299_A_MIN_CALIBRATED_PROB = 0.65


def _decision(prediction, *, tier_a: bool = False):
    from .premium_risk_guard import PremiumRiskDecision

    # Preserve every V2.9.1/V2.9 gate first.
    base = tier_a_decision_v291(prediction) if tier_a else anti_zero_decision_v291(prediction)
    if base is not None:
        return base

    m = anti_zero_metrics(prediction)
    if not m.get("available"):
        return PremiumRiskDecision(True, "v299_evidence_missing", "V2.9.9 evidence unavailable")

    prefix = "v299_a" if tier_a else "v299"
    weakest_floor = V299_A_MIN_WEAKEST_SCORE_PROB if tier_a else V299_MIN_WEAKEST_SCORE_PROB
    max_fts = V299_A_MAX_WEAKEST_OVERALL_FTS if tier_a else V299_MAX_WEAKEST_OVERALL_FTS
    min_recent = V299_A_MIN_WEAKEST_OVERALL_LAST5_SCORED if tier_a else V299_MIN_WEAKEST_OVERALL_LAST5_SCORED

    if float(m["weakest_score_probability"]) < weakest_floor:
        return PremiumRiskDecision(True, f"{prefix}_weak_leg_probability", f"weakest score probability={m['weakest_score_probability']:.1%}<{weakest_floor:.0%}")

    # The Top-3 cannot hide a fragile leg behind the stronger attack.
    for side in ("home", "away"):
        overall = m[f"{side}_overall"]
        if int(overall.get("last5_scored", 0)) < min_recent:
            return PremiumRiskDecision(True, f"{side}_{prefix}_recent_blank_risk", f"{side} scored only {overall.get('last5_scored', 0)}/5")
        if float(overall.get("failed_to_score_rate", 1.0)) > max_fts:
            return PremiumRiskDecision(True, f"{side}_{prefix}_fts_exposure", f"{side} FTS={overall.get('failed_to_score_rate', 1.0):.0%}>{max_fts:.0%}")

    if tier_a:
        if float(m.get("consensus_probability", 0.0)) < V299_A_MIN_CONSENSUS_PROB:
            return PremiumRiskDecision(True, "v299_a_consensus_floor", f"consensus={m.get('consensus_probability', 0.0):.1%}<63%")
        if float(m.get("calibrated_probability", 0.0)) < V299_A_MIN_CALIBRATED_PROB:
            return PremiumRiskDecision(True, "v299_a_calibrated_floor", f"calibrated={m.get('calibrated_probability', 0.0):.1%}<65%")

    c = _opponent_concession_metrics(prediction)
    if not c.get("available"):
        return PremiumRiskDecision(True, f"{prefix}_concession_missing", "opponent concession evidence unavailable")

    min_conceded = V299_A_MIN_OPP_LAST5_CONCEDED if tier_a else V299_MIN_OPP_LAST5_CONCEDED
    max_cs = V299_A_MAX_OPP_LAST5_CLEAN_SHEETS if tier_a else V299_MAX_OPP_LAST5_CLEAN_SHEETS
    for scoring_side, key in (("home", "home_scoring_vs"), ("away", "away_scoring_vs")):
        defence = c[key]
        if int(defence.get("last5_conceded", 0)) < min_conceded:
            return PremiumRiskDecision(True, f"{scoring_side}_{prefix}_clean_sheet_wall", f"opponent conceded only {defence.get('last5_conceded', 0)}/5")
        if int(defence.get("last5_clean_sheets", 5)) > max_cs:
            return PremiumRiskDecision(True, f"{scoring_side}_{prefix}_clean_sheet_wall", f"opponent has {defence.get('last5_clean_sheets', 5)} clean sheets in last 5")

    return None


def anti_zero_decision_v299(prediction):
    return _decision(prediction, tier_a=False)


def tier_a_decision_v299(prediction):
    return _decision(prediction, tier_a=True)


def premium_one_safe_v299(prediction) -> bool:
    return tier_a_decision_v299(prediction) is None


def install_btts_v299_policy() -> None:
    """Install V2.9.9 through the existing production policy hooks."""
    from . import btts_v25_policy
    from .premium_risk_guard import PremiumRiskGuard

    if getattr(PremiumRiskGuard, "_btts_v299_installed", False):
        return
    btts_v25_policy.anti_zero_decision = anti_zero_decision_v299
    btts_v25_policy.tier_a_decision = tier_a_decision_v299
    btts_v25_policy.premium_one_safe = premium_one_safe_v299
    PremiumRiskGuard._btts_v299_installed = True
