from __future__ import annotations

"""BTTS V2.8 precision layer (built on V2.7).

V2.8 keeps the V2.7 anti-zero framework but fixes an important blind spot:
BTTS requires not only that both teams *can score*, but that each opponent
reliably *concedes* in the relevant venue role.

Post-match audit 22/08/2026:
* Tigres 2-0 Atlante: Atlante still produced ~1.14 xG and missed a penalty after
  playing most of the match with ten men. This is largely execution/event
  variance and should NOT trigger a crude hindsight ban.
* Leon 2-0 Monterrey: Monterrey generated ~1.41 xG, 20 shots and 37 touches in
  the opposition box. Again, the process created chances but finishing failed.
* Queretaro 1-2 Toluca landed BTTS, despite the shortest BTTS price.

The durable lesson is therefore not to keep raising attack floors until no
picks survive. The structural improvement is to confirm the *other half* of
each scoring event: opponent concession behaviour. A prolific attack facing a
side that frequently keeps clean sheets must no longer be promoted purely on
its own scoring history.
"""

from .btts_v25_policy import anti_zero_metrics

# Generic Premium floors.
V27_MIN_ROLE_SCORE_RATE = 0.60
V27_MIN_ROLE_AVG_GF = 0.95
V27_MIN_ROLE_LAST5_SCORED = 3
V27_MAX_ROLE_ZERO_RISK = 0.35
V27_MIN_OVERALL_SCORE_RATE = 0.58
V27_MIN_OVERALL_LAST5_SCORED = 3
V27_MIN_WEAKEST_SCORE_PROB = 0.66
V27_MIN_CALIBRATED_PROB = 0.57
V27_MIN_CONSENSUS_PROB = 0.54

# New V2.8 opponent-concession confirmation.
V28_MIN_OPP_CONCEDE_RATE = 0.55
V28_MIN_OPP_AVG_GA = 0.85
V28_MIN_OPP_LAST5_CONCEDED = 3

# Premium A / #1 floors.
V27_A_MIN_ROLE_SCORE_RATE = 0.70
V27_A_MIN_ROLE_AVG_GF = 1.10
V27_A_MIN_ROLE_LAST5_SCORED = 4
V27_A_MAX_ROLE_ZERO_RISK = 0.27
V27_A_MIN_OVERALL_SCORE_RATE = 0.65
V27_A_MIN_OVERALL_LAST5_SCORED = 4
V27_A_MIN_WEAKEST_SCORE_PROB = 0.71
V27_A_MIN_CALIBRATED_PROB = 0.61
V27_A_MIN_CONSENSUS_PROB = 0.58

V28_A_MIN_OPP_CONCEDE_RATE = 0.65
V28_A_MIN_OPP_AVG_GA = 1.00
V28_A_MIN_OPP_LAST5_CONCEDED = 4


def _concession_profile(team, fixture, role: str) -> dict | None:
    """How often *team* concedes when playing the requested venue role.

    For the home side's scoring chance we inspect the away team's away
    concession history. For the away side's scoring chance we inspect the home
    team's home concession history. This makes BTTS explicitly bilateral.
    """
    from .competition_quality import classify_competition
    from .models import Fixture

    filters = dict(
        kickoff__lt=fixture.kickoff,
        home_goals__isnull=False,
        away_goals__isnull=False,
    )
    if role == "home":
        qs = Fixture.objects.filter(home_team=team, **filters)
    else:
        qs = Fixture.objects.filter(away_team=team, **filters)

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
    return {
        "n": len(ga),
        "concede_rate": sum(conceded) / len(conceded),
        "avg_ga": sum(ga) / len(ga),
        "last5_conceded": sum(conceded[:5]),
        "clean_sheet_rate": 1.0 - (sum(conceded) / len(conceded)),
    }


def _opponent_concession_metrics(prediction) -> dict:
    fixture = getattr(prediction, "fixture", None)
    if fixture is None:
        return {"available": False}
    try:
        # Home scoring opportunity depends on away defence in AWAY role.
        away_def = _concession_profile(fixture.away_team, fixture, "away")
        # Away scoring opportunity depends on home defence in HOME role.
        home_def = _concession_profile(fixture.home_team, fixture, "home")
    except Exception:
        return {"available": False}
    if not away_def or not home_def:
        return {"available": False}
    return {
        "available": True,
        "home_scoring_vs": away_def,
        "away_scoring_vs": home_def,
    }


def _decision(prediction, *, tier_a: bool = False):
    from .premium_risk_guard import PremiumRiskDecision

    m = anti_zero_metrics(prediction)
    if not m.get("available"):
        if tier_a:
            return PremiumRiskDecision(True, "v28_a_evidence_missing", "anti-zero evidence unavailable")
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
    prefix = "v28_a" if tier_a else "v28"

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

    if m["weakest_score_probability"] < weakest_floor:
        return PremiumRiskDecision(True, f"{prefix}_weakest_attack", f"weakest scoring probability={m['weakest_score_probability']:.1%}<{weakest_floor:.0%}")
    if m["max_zero_risk"] > max_zero:
        return PremiumRiskDecision(True, f"{prefix}_max_zero_risk", f"max P(0)={m['max_zero_risk']:.1%}>{max_zero:.0%}")
    if m["calibrated_probability"] < calibrated_floor:
        return PremiumRiskDecision(True, f"{prefix}_calibrated_floor", f"calibrated BTTS={m['calibrated_probability']:.1%}<{calibrated_floor:.0%}")
    if m["consensus_probability"] < consensus_floor:
        return PremiumRiskDecision(True, f"{prefix}_consensus_floor", f"bilateral consensus={m['consensus_probability']:.1%}<{consensus_floor:.0%}")

    # V2.8: attack strength must be confirmed by the opponent's tendency to
    # concede in the same venue role. Missing evidence is neutral for generic
    # Premium (recall protection), but Premium A must be fully evidenced.
    c = _opponent_concession_metrics(prediction)
    if not c.get("available"):
        if tier_a:
            return PremiumRiskDecision(True, "v28_a_concession_evidence_missing", "opponent concession evidence unavailable")
        return None

    min_concede = V28_A_MIN_OPP_CONCEDE_RATE if tier_a else V28_MIN_OPP_CONCEDE_RATE
    min_avg_ga = V28_A_MIN_OPP_AVG_GA if tier_a else V28_MIN_OPP_AVG_GA
    min_last5 = V28_A_MIN_OPP_LAST5_CONCEDED if tier_a else V28_MIN_OPP_LAST5_CONCEDED

    for scoring_side, key in (("home", "home_scoring_vs"), ("away", "away_scoring_vs")):
        d = c[key]
        if d["concede_rate"] < min_concede:
            return PremiumRiskDecision(True, f"{scoring_side}_{prefix}_opp_clean_sheet", f"opponent concedes {d['concede_rate']:.0%}<{min_concede:.0%} in venue role")
        if d["avg_ga"] < min_avg_ga:
            return PremiumRiskDecision(True, f"{scoring_side}_{prefix}_opp_low_ga", f"opponent avgGA={d['avg_ga']:.2f}<{min_avg_ga:.2f} in venue role")
        if d["last5_conceded"] < min_last5:
            return PremiumRiskDecision(True, f"{scoring_side}_{prefix}_opp_recent_clean_sheets", f"opponent conceded {d['last5_conceded']}/5<{min_last5}")

    return None


def anti_zero_decision_v27(prediction):
    return _decision(prediction, tier_a=False)


def tier_a_decision_v27(prediction):
    return _decision(prediction, tier_a=True)


def premium_one_safe_v27(prediction) -> bool:
    """Top Premium is allowed only if it clears the stricter V2.8 A gate."""
    return tier_a_decision_v27(prediction) is None


def install_btts_v27_policy() -> None:
    """Install V2.8 through the existing V2.7 hook to keep imports stable."""
    from . import btts_v25_policy
    from .premium_risk_guard import PremiumRiskGuard

    if getattr(PremiumRiskGuard, "_btts_v27_installed", False):
        return

    btts_v25_policy.anti_zero_decision = anti_zero_decision_v27
    btts_v25_policy.tier_a_decision = tier_a_decision_v27
    btts_v25_policy.premium_one_safe = premium_one_safe_v27

    PremiumRiskGuard._btts_v27_installed = True
