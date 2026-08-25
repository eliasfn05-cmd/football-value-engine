from __future__ import annotations

"""BTTS V2.9 precision layer.

V2.9 keeps the V2.8 bilateral opponent-concession confirmation and adds a
strict weakest-link publication gate. BTTS needs two independent scoring
legs, so a strong attack must never average away a structurally weak one.
Candidates failing two critical bilateral checks are hidden rather than
published as Premium B/watchlist material.
"""

from .btts_v25_policy import anti_zero_metrics

# Generic Premium floors (V2.9).
V29_MIN_ROLE_SCORE_RATE = 0.60
V29_MIN_ROLE_AVG_GF = 0.95
V29_MIN_ROLE_LAST5_SCORED = 4
V29_MAX_ROLE_ZERO_RISK = 0.35
V29_MIN_OVERALL_SCORE_RATE = 0.58
V29_MIN_OVERALL_LAST5_SCORED = 4
V29_MIN_WEAKEST_SCORE_PROB = 0.65
V29_MIN_CALIBRATED_PROB = 0.60
V29_MIN_CONSENSUS_PROB = 0.57

# Opponent-concession confirmation.
V29_MIN_OPP_CONCEDE_RATE = 0.55
V29_MIN_OPP_AVG_GA = 0.85
V29_MIN_OPP_LAST5_CONCEDED = 3
V29_MAX_OPP_LAST5_CLEAN_SHEETS = 2

# Premium A / #1 floors.
V29_A_MIN_ROLE_SCORE_RATE = 0.70
V29_A_MIN_ROLE_AVG_GF = 1.10
V29_A_MIN_ROLE_LAST5_SCORED = 4
V29_A_MAX_ROLE_ZERO_RISK = 0.27
V29_A_MIN_OVERALL_SCORE_RATE = 0.65
V29_A_MIN_OVERALL_LAST5_SCORED = 4
V29_A_MIN_WEAKEST_SCORE_PROB = 0.71
V29_A_MIN_CALIBRATED_PROB = 0.64
V29_A_MIN_CONSENSUS_PROB = 0.60
V29_A_MIN_OPP_CONCEDE_RATE = 0.65
V29_A_MIN_OPP_AVG_GA = 1.00
V29_A_MIN_OPP_LAST5_CONCEDED = 4
V29_A_MAX_OPP_LAST5_CLEAN_SHEETS = 1


def _concession_profile(team, fixture, role: str) -> dict | None:
    from .competition_quality import classify_competition
    from .models import Fixture

    filters = dict(kickoff__lt=fixture.kickoff, home_goals__isnull=False, away_goals__isnull=False)
    qs = Fixture.objects.filter(home_team=team, **filters) if role == "home" else Fixture.objects.filter(away_team=team, **filters)
    qs = qs.select_related("competition_ref", "home_team", "away_team").order_by("-kickoff")
    ga: list[int] = []
    for previous in qs.iterator(chunk_size=50):
        if classify_competition(previous).excluded:
            continue
        goals_against = int(previous.away_goals or 0) if role == "home" else int(previous.home_goals or 0)
        ga.append(goals_against)
        if len(ga) >= 10:
            break
    if len(ga) < 5:
        return None
    conceded = [int(v > 0) for v in ga]
    last5_conceded = sum(conceded[:5])
    return {
        "n": len(ga),
        "concede_rate": sum(conceded) / len(conceded),
        "avg_ga": sum(ga) / len(ga),
        "last5_conceded": last5_conceded,
        "last5_clean_sheets": 5 - last5_conceded,
        "clean_sheet_rate": 1.0 - (sum(conceded) / len(conceded)),
    }


def _opponent_concession_metrics(prediction) -> dict:
    fixture = getattr(prediction, "fixture", None)
    if fixture is None:
        return {"available": False}
    try:
        away_def = _concession_profile(fixture.away_team, fixture, "away")
        home_def = _concession_profile(fixture.home_team, fixture, "home")
    except Exception:
        return {"available": False}
    if not away_def or not home_def:
        return {"available": False}
    return {"available": True, "home_scoring_vs": away_def, "away_scoring_vs": home_def}


def _decision(prediction, *, tier_a: bool = False):
    from .premium_risk_guard import PremiumRiskDecision

    m = anti_zero_metrics(prediction)
    if not m.get("available"):
        return PremiumRiskDecision(True, "v29_evidence_missing", "V2.9 bilateral anti-zero evidence unavailable")

    role_score = V29_A_MIN_ROLE_SCORE_RATE if tier_a else V29_MIN_ROLE_SCORE_RATE
    role_avg = V29_A_MIN_ROLE_AVG_GF if tier_a else V29_MIN_ROLE_AVG_GF
    role_last5 = V29_A_MIN_ROLE_LAST5_SCORED if tier_a else V29_MIN_ROLE_LAST5_SCORED
    max_zero = V29_A_MAX_ROLE_ZERO_RISK if tier_a else V29_MAX_ROLE_ZERO_RISK
    overall_score = V29_A_MIN_OVERALL_SCORE_RATE if tier_a else V29_MIN_OVERALL_SCORE_RATE
    overall_last5 = V29_A_MIN_OVERALL_LAST5_SCORED if tier_a else V29_MIN_OVERALL_LAST5_SCORED
    weakest_floor = V29_A_MIN_WEAKEST_SCORE_PROB if tier_a else V29_MIN_WEAKEST_SCORE_PROB
    calibrated_floor = V29_A_MIN_CALIBRATED_PROB if tier_a else V29_MIN_CALIBRATED_PROB
    consensus_floor = V29_A_MIN_CONSENSUS_PROB if tier_a else V29_MIN_CONSENSUS_PROB
    prefix = "v29_a" if tier_a else "v29"

    # Weakest-link gate: each scoring leg must independently satisfy the model.
    for side in ("home", "away"):
        p = m[side]
        failures = []
        if p["score_rate"] < role_score: failures.append("role_score_rate")
        if p["avg_gf"] < role_avg: failures.append("role_avg_gf")
        if p["last5_scored"] < role_last5: failures.append("recent_scoring")
        if p["zero_risk"] > max_zero: failures.append("zero_risk")
        overall = m[f"{side}_overall"]
        if overall["score_rate"] < overall_score: failures.append("overall_score_rate")
        if overall["last5_scored"] < overall_last5: failures.append("overall_recent_scoring")
        if len(failures) >= 2:
            return PremiumRiskDecision(True, f"{side}_{prefix}_weakest_link", f"{side} fails critical bilateral gates: {','.join(failures)}")
        if failures:
            return PremiumRiskDecision(True, f"{side}_{prefix}_{failures[0]}", f"{side} fails V2.9 bilateral gate: {failures[0]}")

    if m["weakest_score_probability"] < weakest_floor:
        return PremiumRiskDecision(True, f"{prefix}_weakest_attack", f"weakest scoring probability={m['weakest_score_probability']:.1%}<{weakest_floor:.0%}")
    if m["max_zero_risk"] > max_zero:
        return PremiumRiskDecision(True, f"{prefix}_max_zero_risk", f"max P(0)={m['max_zero_risk']:.1%}>{max_zero:.0%}")
    if m["calibrated_probability"] < calibrated_floor:
        return PremiumRiskDecision(True, f"{prefix}_calibrated_floor", f"calibrated BTTS={m['calibrated_probability']:.1%}<{calibrated_floor:.0%}")
    if m["consensus_probability"] < consensus_floor:
        return PremiumRiskDecision(True, f"{prefix}_consensus_floor", f"bilateral consensus={m['consensus_probability']:.1%}<{consensus_floor:.0%}")

    c = _opponent_concession_metrics(prediction)
    if not c.get("available"):
        return PremiumRiskDecision(True, f"{prefix}_concession_evidence_missing", "opponent concession evidence unavailable")

    min_concede = V29_A_MIN_OPP_CONCEDE_RATE if tier_a else V29_MIN_OPP_CONCEDE_RATE
    min_avg_ga = V29_A_MIN_OPP_AVG_GA if tier_a else V29_MIN_OPP_AVG_GA
    min_last5 = V29_A_MIN_OPP_LAST5_CONCEDED if tier_a else V29_MIN_OPP_LAST5_CONCEDED
    max_cs5 = V29_A_MAX_OPP_LAST5_CLEAN_SHEETS if tier_a else V29_MAX_OPP_LAST5_CLEAN_SHEETS

    for scoring_side, key in (("home", "home_scoring_vs"), ("away", "away_scoring_vs")):
        d = c[key]
        failures = []
        if d["concede_rate"] < min_concede: failures.append("opp_concede_rate")
        if d["avg_ga"] < min_avg_ga: failures.append("opp_avg_ga")
        if d["last5_conceded"] < min_last5: failures.append("opp_recent_concession")
        if d["last5_clean_sheets"] > max_cs5: failures.append("opp_clean_sheet_wall")
        if failures:
            return PremiumRiskDecision(True, f"{scoring_side}_{prefix}_{failures[0]}", f"{scoring_side} scoring leg fails V2.9: {','.join(failures)}")

    return None


def anti_zero_decision_v27(prediction):
    return _decision(prediction, tier_a=False)


def tier_a_decision_v27(prediction):
    return _decision(prediction, tier_a=True)


def premium_one_safe_v27(prediction) -> bool:
    return tier_a_decision_v27(prediction) is None


def install_btts_v27_policy() -> None:
    """Install V2.9 through the stable V2.7 hook used by production."""
    from . import btts_v25_policy
    from .premium_risk_guard import PremiumRiskGuard

    if getattr(PremiumRiskGuard, "_btts_v27_installed", False):
        return
    btts_v25_policy.anti_zero_decision = anti_zero_decision_v27
    btts_v25_policy.tier_a_decision = tier_a_decision_v27
    btts_v25_policy.premium_one_safe = premium_one_safe_v27
    PremiumRiskGuard._btts_v27_installed = True
