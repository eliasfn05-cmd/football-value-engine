from __future__ import annotations

"""BTTS V2.9.10 hard anti-zero guard.

Post-mortem target: Jong Utrecht - FC Eindhoven 0-0 (2026-09-07).

V2.9.10 is intentionally conservative. It does not attempt to make 0-0
impossible; instead it converts any profile with meaningful recent blank,
clean-sheet or weakest-leg evidence into NO BET. The objective is precision,
not selection volume.

This layer is additive over V2.9.9 -> V2.9.2 -> V2.9.1 -> V2.9.
"""

from .btts_v25_policy import anti_zero_metrics
from .btts_v27_policy import _opponent_concession_metrics
from .btts_v299_policy import anti_zero_decision_v299, tier_a_decision_v299

# Generic published BTTS candidate. These are deliberately stricter than V2.9.9.
V2910_MIN_WEAKEST_SCORE_PROB = 0.74
V2910_MAX_ZERO_RISK = 0.26
V2910_MIN_CONSENSUS = 0.61
V2910_MIN_CALIBRATED = 0.63
V2910_MIN_EMPIRICAL_BTTS = 0.55
V2910_MAX_ROLE_FTS = 0.25
V2910_MAX_OVERALL_FTS = 0.20
V2910_MIN_ROLE_LAST5_SCORED = 4
V2910_MIN_OVERALL_LAST5_SCORED = 4
V2910_MIN_OVERALL_LAST5_BTTS = 3
V2910_MIN_OPP_CONCEDE_RATE = 0.60
V2910_MIN_OPP_LAST5_CONCEDED = 4
V2910_MAX_OPP_LAST5_CLEAN_SHEETS = 1

# Tier A / Top-3. A candidate with any recent zero-goal warning is rejected.
V2910_A_MIN_WEAKEST_SCORE_PROB = 0.77
V2910_A_MAX_ZERO_RISK = 0.22
V2910_A_MIN_CONSENSUS = 0.66
V2910_A_MIN_CALIBRATED = 0.68
V2910_A_MIN_EMPIRICAL_BTTS = 0.60
V2910_A_MAX_ROLE_FTS = 0.20
V2910_A_MAX_OVERALL_FTS = 0.15
V2910_A_MIN_ROLE_LAST5_SCORED = 5
V2910_A_MIN_OVERALL_LAST5_SCORED = 5
V2910_A_MIN_OVERALL_LAST5_BTTS = 4
V2910_A_MIN_OPP_CONCEDE_RATE = 0.70
V2910_A_MIN_OPP_LAST5_CONCEDED = 5
V2910_A_MAX_OPP_LAST5_CLEAN_SHEETS = 0


def _decision(prediction, *, tier_a: bool = False):
    from .premium_risk_guard import PremiumRiskDecision

    # Preserve every prior production gate first.
    prior = tier_a_decision_v299(prediction) if tier_a else anti_zero_decision_v299(prediction)
    if prior is not None:
        return prior

    m = anti_zero_metrics(prediction)
    if not m.get("available"):
        return PremiumRiskDecision(True, "v2910_evidence_missing", "V2.9.10 anti-zero evidence unavailable")

    prefix = "v2910_a" if tier_a else "v2910"
    weakest_floor = V2910_A_MIN_WEAKEST_SCORE_PROB if tier_a else V2910_MIN_WEAKEST_SCORE_PROB
    max_zero = V2910_A_MAX_ZERO_RISK if tier_a else V2910_MAX_ZERO_RISK
    min_consensus = V2910_A_MIN_CONSENSUS if tier_a else V2910_MIN_CONSENSUS
    min_calibrated = V2910_A_MIN_CALIBRATED if tier_a else V2910_MIN_CALIBRATED
    min_empirical = V2910_A_MIN_EMPIRICAL_BTTS if tier_a else V2910_MIN_EMPIRICAL_BTTS
    max_role_fts = V2910_A_MAX_ROLE_FTS if tier_a else V2910_MAX_ROLE_FTS
    max_overall_fts = V2910_A_MAX_OVERALL_FTS if tier_a else V2910_MAX_OVERALL_FTS
    min_role_l5 = V2910_A_MIN_ROLE_LAST5_SCORED if tier_a else V2910_MIN_ROLE_LAST5_SCORED
    min_overall_l5 = V2910_A_MIN_OVERALL_LAST5_SCORED if tier_a else V2910_MIN_OVERALL_LAST5_SCORED
    min_overall_btts = V2910_A_MIN_OVERALL_LAST5_BTTS if tier_a else V2910_MIN_OVERALL_LAST5_BTTS

    if float(m.get("weakest_score_probability", 0.0)) < weakest_floor:
        return PremiumRiskDecision(True, f"{prefix}_weakest_score_floor", f"weakest score probability={m.get('weakest_score_probability', 0.0):.1%}<{weakest_floor:.0%}")
    if float(m.get("max_zero_risk", 1.0)) > max_zero:
        return PremiumRiskDecision(True, f"{prefix}_zero_risk_cap", f"max zero-risk={m.get('max_zero_risk', 1.0):.1%}>{max_zero:.0%}")
    if float(m.get("consensus_probability", 0.0)) < min_consensus:
        return PremiumRiskDecision(True, f"{prefix}_consensus_floor", f"consensus={m.get('consensus_probability', 0.0):.1%}<{min_consensus:.0%}")
    if float(m.get("calibrated_probability", 0.0)) < min_calibrated:
        return PremiumRiskDecision(True, f"{prefix}_calibrated_floor", f"calibrated={m.get('calibrated_probability', 0.0):.1%}<{min_calibrated:.0%}")
    if float(m.get("empirical_btts", 0.0)) < min_empirical:
        return PremiumRiskDecision(True, f"{prefix}_empirical_btts_floor", f"empirical BTTS={m.get('empirical_btts', 0.0):.1%}<{min_empirical:.0%}")

    # Both attacks must be hot in the exact venue role and overall. A single
    # weak scoring leg is enough to hide the match from the published list.
    for side in ("home", "away"):
        role = m[side]
        overall = m[f"{side}_overall"]
        if float(role.get("failed_to_score_rate", 1.0)) > max_role_fts:
            return PremiumRiskDecision(True, f"{side}_{prefix}_role_fts", f"{side} role FTS={role.get('failed_to_score_rate', 1.0):.0%}>{max_role_fts:.0%}")
        if float(overall.get("failed_to_score_rate", 1.0)) > max_overall_fts:
            return PremiumRiskDecision(True, f"{side}_{prefix}_overall_fts", f"{side} overall FTS={overall.get('failed_to_score_rate', 1.0):.0%}>{max_overall_fts:.0%}")
        if int(role.get("last5_scored", 0)) < min_role_l5:
            return PremiumRiskDecision(True, f"{side}_{prefix}_role_recent_blank", f"{side} role scored {role.get('last5_scored', 0)}/5<{min_role_l5}/5")
        if int(overall.get("last5_scored", 0)) < min_overall_l5:
            return PremiumRiskDecision(True, f"{side}_{prefix}_overall_recent_blank", f"{side} overall scored {overall.get('last5_scored', 0)}/5<{min_overall_l5}/5")
        if int(overall.get("last5_btts", 0)) < min_overall_btts:
            return PremiumRiskDecision(True, f"{side}_{prefix}_recent_btts_floor", f"{side} recent BTTS participation={overall.get('last5_btts', 0)}/5<{min_overall_btts}/5")

    # For each scoring leg, the opponent must also show repeated inability to
    # keep clean sheets. Tier A requires five straight recent concessions in
    # the exact defensive venue role; otherwise the match is not Top-3 safe.
    c = _opponent_concession_metrics(prediction)
    if not c.get("available"):
        return PremiumRiskDecision(True, f"{prefix}_concession_missing", "opponent concession evidence unavailable")

    min_concede_rate = V2910_A_MIN_OPP_CONCEDE_RATE if tier_a else V2910_MIN_OPP_CONCEDE_RATE
    min_l5_conceded = V2910_A_MIN_OPP_LAST5_CONCEDED if tier_a else V2910_MIN_OPP_LAST5_CONCEDED
    max_l5_cs = V2910_A_MAX_OPP_LAST5_CLEAN_SHEETS if tier_a else V2910_MAX_OPP_LAST5_CLEAN_SHEETS
    for scoring_side, key in (("home", "home_scoring_vs"), ("away", "away_scoring_vs")):
        defence = c[key]
        if float(defence.get("concede_rate", 0.0)) < min_concede_rate:
            return PremiumRiskDecision(True, f"{scoring_side}_{prefix}_opp_concede_rate", f"opponent concede rate={defence.get('concede_rate', 0.0):.0%}<{min_concede_rate:.0%}")
        if int(defence.get("last5_conceded", 0)) < min_l5_conceded:
            return PremiumRiskDecision(True, f"{scoring_side}_{prefix}_opp_recent_clean_sheet", f"opponent conceded {defence.get('last5_conceded', 0)}/5<{min_l5_conceded}/5")
        if int(defence.get("last5_clean_sheets", 5)) > max_l5_cs:
            return PremiumRiskDecision(True, f"{scoring_side}_{prefix}_opp_clean_sheet_wall", f"opponent clean sheets={defence.get('last5_clean_sheets', 5)}/5>{max_l5_cs}")

    return None


def anti_zero_decision_v2910(prediction):
    return _decision(prediction, tier_a=False)


def tier_a_decision_v2910(prediction):
    return _decision(prediction, tier_a=True)


def premium_one_safe_v2910(prediction) -> bool:
    return tier_a_decision_v2910(prediction) is None


def install_btts_v2910_policy() -> None:
    """Install V2.9.10 as the final production BTTS precision layer."""
    from . import btts_v25_policy
    from .premium_risk_guard import PremiumRiskGuard

    if getattr(PremiumRiskGuard, "_btts_v2910_installed", False):
        return
    btts_v25_policy.anti_zero_decision = anti_zero_decision_v2910
    btts_v25_policy.tier_a_decision = tier_a_decision_v2910
    btts_v25_policy.premium_one_safe = premium_one_safe_v2910
    PremiumRiskGuard._btts_v2910_installed = True
