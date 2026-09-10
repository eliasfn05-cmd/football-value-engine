from __future__ import annotations

"""BTTS V2.9.14 weakest-attack confirmation guard.

Post-mortem target: Hradec Kralove - Viktoria Plzen 0-0 (2026-09-09).

V2.9.13 is strong on total-goal environment, but a BTTS pick can still be
misleading when one side's own scoring production is too weak. V2.9.14 adds an
independent team-scoring confirmation layer: both teams must repeatedly score
in their exact venue role and overall, with minimum own-goal averages.

The aim is to reduce 0-0 and one-sided BTTS failures. It intentionally accepts
more NO BET outcomes and does not guarantee future goals.
"""

from django.db.models import Q

from .btts_v2913_policy import anti_zero_decision_v2913, tier_a_decision_v2913

# Generic published candidate.
V2914_MIN_ROLE_LAST5_SCORED = 4
V2914_MIN_OVERALL_LAST5_SCORED = 4
V2914_MIN_ROLE_LAST10_SCORED = 8
V2914_MIN_OVERALL_LAST10_SCORED = 8
V2914_MIN_ROLE_AVG_GF = 1.25
V2914_MIN_OVERALL_AVG_GF = 1.20
V2914_MAX_ROLE_LAST10_BLANKS = 2
V2914_MAX_OVERALL_LAST10_BLANKS = 2

# Tier A / Top-3: weakest attack must be highly reliable.
V2914_A_MIN_ROLE_LAST5_SCORED = 5
V2914_A_MIN_OVERALL_LAST5_SCORED = 5
V2914_A_MIN_ROLE_LAST10_SCORED = 9
V2914_A_MIN_OVERALL_LAST10_SCORED = 9
V2914_A_MIN_ROLE_AVG_GF = 1.45
V2914_A_MIN_OVERALL_AVG_GF = 1.35
V2914_A_MAX_ROLE_LAST10_BLANKS = 1
V2914_A_MAX_OVERALL_LAST10_BLANKS = 1


def _scoring_profile(team, fixture, role: str | None) -> dict | None:
    from .competition_quality import classify_competition
    from .models import Fixture

    filters = dict(
        kickoff__lt=fixture.kickoff,
        home_goals__isnull=False,
        away_goals__isnull=False,
    )
    if role == "home":
        qs = Fixture.objects.filter(home_team=team, **filters)
    elif role == "away":
        qs = Fixture.objects.filter(away_team=team, **filters)
    else:
        qs = Fixture.objects.filter(Q(home_team=team) | Q(away_team=team), **filters)

    qs = qs.select_related("competition_ref", "home_team", "away_team").order_by("-kickoff")
    goals_for: list[int] = []
    for previous in qs.iterator(chunk_size=50):
        if classify_competition(previous).excluded:
            continue
        gf = int(previous.home_goals or 0) if previous.home_team_id == team.id else int(previous.away_goals or 0)
        goals_for.append(gf)
        if len(goals_for) >= 10:
            break

    if len(goals_for) < 10:
        return None
    last5 = goals_for[:5]
    return {
        "n": len(goals_for),
        "last5_scored": sum(int(v >= 1) for v in last5),
        "last10_scored": sum(int(v >= 1) for v in goals_for),
        "last10_blanks": sum(int(v == 0) for v in goals_for),
        "avg_gf": sum(goals_for) / len(goals_for),
        "last5_avg_gf": sum(last5) / len(last5),
        "goals_for": goals_for,
    }


def v2914_scoring_metrics(prediction) -> dict:
    fixture = getattr(prediction, "fixture", None)
    if fixture is None:
        return {"available": False}

    try:
        home_role = _scoring_profile(fixture.home_team, fixture, "home")
        away_role = _scoring_profile(fixture.away_team, fixture, "away")
        home_overall = _scoring_profile(fixture.home_team, fixture, None)
        away_overall = _scoring_profile(fixture.away_team, fixture, None)
    except Exception:
        return {"available": False}

    if not all((home_role, away_role, home_overall, away_overall)):
        return {"available": False}

    return {
        "available": True,
        "home_role": home_role,
        "away_role": away_role,
        "home_overall": home_overall,
        "away_overall": away_overall,
        "min_role_last5_scored": min(home_role["last5_scored"], away_role["last5_scored"]),
        "min_overall_last5_scored": min(home_overall["last5_scored"], away_overall["last5_scored"]),
        "min_role_last10_scored": min(home_role["last10_scored"], away_role["last10_scored"]),
        "min_overall_last10_scored": min(home_overall["last10_scored"], away_overall["last10_scored"]),
        "min_role_avg_gf": min(home_role["avg_gf"], away_role["avg_gf"]),
        "min_overall_avg_gf": min(home_overall["avg_gf"], away_overall["avg_gf"]),
        "max_role_last10_blanks": max(home_role["last10_blanks"], away_role["last10_blanks"]),
        "max_overall_last10_blanks": max(home_overall["last10_blanks"], away_overall["last10_blanks"]),
    }


def _decision(prediction, *, tier_a: bool = False):
    from .premium_risk_guard import PremiumRiskDecision

    prior = tier_a_decision_v2913(prediction) if tier_a else anti_zero_decision_v2913(prediction)
    if prior is not None:
        return prior

    m = v2914_scoring_metrics(prediction)
    if not m.get("available"):
        return PremiumRiskDecision(True, "v2914_scoring_evidence_missing", "V2.9.14 team-scoring evidence unavailable")

    prefix = "v2914_a" if tier_a else "v2914"
    min_role5 = V2914_A_MIN_ROLE_LAST5_SCORED if tier_a else V2914_MIN_ROLE_LAST5_SCORED
    min_overall5 = V2914_A_MIN_OVERALL_LAST5_SCORED if tier_a else V2914_MIN_OVERALL_LAST5_SCORED
    min_role10 = V2914_A_MIN_ROLE_LAST10_SCORED if tier_a else V2914_MIN_ROLE_LAST10_SCORED
    min_overall10 = V2914_A_MIN_OVERALL_LAST10_SCORED if tier_a else V2914_MIN_OVERALL_LAST10_SCORED
    min_role_avg = V2914_A_MIN_ROLE_AVG_GF if tier_a else V2914_MIN_ROLE_AVG_GF
    min_overall_avg = V2914_A_MIN_OVERALL_AVG_GF if tier_a else V2914_MIN_OVERALL_AVG_GF
    max_role_blanks = V2914_A_MAX_ROLE_LAST10_BLANKS if tier_a else V2914_MAX_ROLE_LAST10_BLANKS
    max_overall_blanks = V2914_A_MAX_OVERALL_LAST10_BLANKS if tier_a else V2914_MAX_OVERALL_LAST10_BLANKS

    if int(m["min_role_last5_scored"]) < min_role5:
        return PremiumRiskDecision(True, f"{prefix}_role_last5_scoring", f"weakest role scored={m['min_role_last5_scored']}/5<{min_role5}/5")
    if int(m["min_overall_last5_scored"]) < min_overall5:
        return PremiumRiskDecision(True, f"{prefix}_overall_last5_scoring", f"weakest overall scored={m['min_overall_last5_scored']}/5<{min_overall5}/5")
    if int(m["min_role_last10_scored"]) < min_role10:
        return PremiumRiskDecision(True, f"{prefix}_role_last10_scoring", f"weakest role scored={m['min_role_last10_scored']}/10<{min_role10}/10")
    if int(m["min_overall_last10_scored"]) < min_overall10:
        return PremiumRiskDecision(True, f"{prefix}_overall_last10_scoring", f"weakest overall scored={m['min_overall_last10_scored']}/10<{min_overall10}/10")
    if float(m["min_role_avg_gf"]) < min_role_avg:
        return PremiumRiskDecision(True, f"{prefix}_role_avg_gf", f"weakest role avg GF={m['min_role_avg_gf']:.2f}<{min_role_avg:.2f}")
    if float(m["min_overall_avg_gf"]) < min_overall_avg:
        return PremiumRiskDecision(True, f"{prefix}_overall_avg_gf", f"weakest overall avg GF={m['min_overall_avg_gf']:.2f}<{min_overall_avg:.2f}")
    if int(m["max_role_last10_blanks"]) > max_role_blanks:
        return PremiumRiskDecision(True, f"{prefix}_role_blanks", f"role scoring blanks={m['max_role_last10_blanks']}/10>{max_role_blanks}/10")
    if int(m["max_overall_last10_blanks"]) > max_overall_blanks:
        return PremiumRiskDecision(True, f"{prefix}_overall_blanks", f"overall scoring blanks={m['max_overall_last10_blanks']}/10>{max_overall_blanks}/10")

    return None


def anti_zero_decision_v2914(prediction):
    return _decision(prediction, tier_a=False)


def tier_a_decision_v2914(prediction):
    return _decision(prediction, tier_a=True)


def premium_one_safe_v2914(prediction) -> bool:
    return tier_a_decision_v2914(prediction) is None


def install_btts_v2914_policy() -> None:
    from . import btts_v25_policy
    from .premium_risk_guard import PremiumRiskGuard

    if getattr(PremiumRiskGuard, "_btts_v2914_installed", False):
        return
    btts_v25_policy.anti_zero_decision = anti_zero_decision_v2914
    btts_v25_policy.tier_a_decision = tier_a_decision_v2914
    btts_v25_policy.premium_one_safe = premium_one_safe_v2914
    PremiumRiskGuard._btts_v2914_installed = True
