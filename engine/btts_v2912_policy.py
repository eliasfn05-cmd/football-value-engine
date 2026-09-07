from __future__ import annotations

"""BTTS V2.9.12 hard low-total exclusion.

Post-mortem target: Orebro - Ostersund 0-1 (2026-09-07).

BTTS requires at least two total goals. V2.9.12 adds a distributional hard gate
on top of V2.9.11: recent matches in the exact venue roles and overall must
repeatedly clear 1.5 goals. A candidate with recent 0-0 / 1-0 / 0-1 patterns is
converted to NO BET even if its BTTS probability looks attractive.

The objective is precision, not volume. This cannot guarantee two goals in a
future match; it only excludes profiles with meaningful pre-match low-total
risk.
"""

from .btts_v2911_policy import (
    anti_zero_decision_v2911,
    tier_a_decision_v2911,
    v2911_goal_environment_metrics,
)

# Generic published candidate: strong minimum-two-goal environment.
V2912_MIN_TOTAL_LAMBDA = 2.70
V2912_MIN_P_GE_2_GOALS = 0.75
V2912_MIN_WEAKEST_GOAL_LAMBDA = 1.05
V2912_MIN_ROLE_LAST5_GE2 = 4
V2912_MIN_OVERALL_LAST5_GE2 = 4
V2912_MIN_ROLE_LAST3_GE2 = 3
V2912_MIN_OVERALL_LAST3_GE2 = 3
V2912_MIN_ROLE_AVG_TOTAL = 2.50
V2912_MIN_OVERALL_AVG_TOTAL = 2.40

# Tier A / Top-3: no recent <=1-goal warning is tolerated.
V2912_A_MIN_TOTAL_LAMBDA = 3.00
V2912_A_MIN_P_GE_2_GOALS = 0.80
V2912_A_MIN_WEAKEST_GOAL_LAMBDA = 1.20
V2912_A_MIN_ROLE_LAST5_GE2 = 5
V2912_A_MIN_OVERALL_LAST5_GE2 = 5
V2912_A_MIN_ROLE_LAST3_GE2 = 3
V2912_A_MIN_OVERALL_LAST3_GE2 = 3
V2912_A_MIN_ROLE_AVG_TOTAL = 2.80
V2912_A_MIN_OVERALL_AVG_TOTAL = 2.70


def _recent_total_profile(team, fixture, role: str | None) -> dict | None:
    from django.db.models import Q
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
    totals: list[int] = []
    for previous in qs.iterator(chunk_size=50):
        if classify_competition(previous).excluded:
            continue
        totals.append(int(previous.home_goals or 0) + int(previous.away_goals or 0))
        if len(totals) >= 10:
            break

    if len(totals) < 5:
        return None
    last5 = totals[:5]
    last3 = totals[:3]
    return {
        "n": len(totals),
        "last5_ge2": sum(int(v >= 2) for v in last5),
        "last3_ge2": sum(int(v >= 2) for v in last3),
        "last5_low_total": sum(int(v <= 1) for v in last5),
        "last3_low_total": sum(int(v <= 1) for v in last3),
        "avg_total": sum(totals) / len(totals),
        "last5_avg_total": sum(last5) / len(last5),
        "totals": totals,
    }


def v2912_low_total_metrics(prediction) -> dict:
    fixture = getattr(prediction, "fixture", None)
    if fixture is None:
        return {"available": False}

    g = v2911_goal_environment_metrics(prediction)
    if not g.get("available"):
        return {"available": False}

    try:
        home_role = _recent_total_profile(fixture.home_team, fixture, "home")
        away_role = _recent_total_profile(fixture.away_team, fixture, "away")
        home_overall = _recent_total_profile(fixture.home_team, fixture, None)
        away_overall = _recent_total_profile(fixture.away_team, fixture, None)
    except Exception:
        return {"available": False}

    if not all((home_role, away_role, home_overall, away_overall)):
        return {"available": False}

    return {
        "available": True,
        "goal_environment": g,
        "home_role": home_role,
        "away_role": away_role,
        "home_overall": home_overall,
        "away_overall": away_overall,
        "min_role_last5_ge2": min(home_role["last5_ge2"], away_role["last5_ge2"]),
        "min_overall_last5_ge2": min(home_overall["last5_ge2"], away_overall["last5_ge2"]),
        "min_role_last3_ge2": min(home_role["last3_ge2"], away_role["last3_ge2"]),
        "min_overall_last3_ge2": min(home_overall["last3_ge2"], away_overall["last3_ge2"]),
        "min_role_avg_total": min(home_role["avg_total"], away_role["avg_total"]),
        "min_overall_avg_total": min(home_overall["avg_total"], away_overall["avg_total"]),
        "max_role_last5_low_total": max(home_role["last5_low_total"], away_role["last5_low_total"]),
        "max_overall_last5_low_total": max(home_overall["last5_low_total"], away_overall["last5_low_total"]),
    }


def _decision(prediction, *, tier_a: bool = False):
    from .premium_risk_guard import PremiumRiskDecision

    prior = tier_a_decision_v2911(prediction) if tier_a else anti_zero_decision_v2911(prediction)
    if prior is not None:
        return prior

    m = v2912_low_total_metrics(prediction)
    if not m.get("available"):
        return PremiumRiskDecision(True, "v2912_low_total_evidence_missing", "V2.9.12 low-total evidence unavailable")

    g = m["goal_environment"]
    prefix = "v2912_a" if tier_a else "v2912"
    min_total = V2912_A_MIN_TOTAL_LAMBDA if tier_a else V2912_MIN_TOTAL_LAMBDA
    min_p2 = V2912_A_MIN_P_GE_2_GOALS if tier_a else V2912_MIN_P_GE_2_GOALS
    min_weak = V2912_A_MIN_WEAKEST_GOAL_LAMBDA if tier_a else V2912_MIN_WEAKEST_GOAL_LAMBDA
    min_role5 = V2912_A_MIN_ROLE_LAST5_GE2 if tier_a else V2912_MIN_ROLE_LAST5_GE2
    min_overall5 = V2912_A_MIN_OVERALL_LAST5_GE2 if tier_a else V2912_MIN_OVERALL_LAST5_GE2
    min_role3 = V2912_A_MIN_ROLE_LAST3_GE2 if tier_a else V2912_MIN_ROLE_LAST3_GE2
    min_overall3 = V2912_A_MIN_OVERALL_LAST3_GE2 if tier_a else V2912_MIN_OVERALL_LAST3_GE2
    min_role_avg = V2912_A_MIN_ROLE_AVG_TOTAL if tier_a else V2912_MIN_ROLE_AVG_TOTAL
    min_overall_avg = V2912_A_MIN_OVERALL_AVG_TOTAL if tier_a else V2912_MIN_OVERALL_AVG_TOTAL

    if float(g.get("total_goal_lambda", 0.0)) < min_total:
        return PremiumRiskDecision(True, f"{prefix}_total_lambda", f"total lambda={g.get('total_goal_lambda', 0.0):.2f}<{min_total:.2f}")
    if float(g.get("p_ge_2_goals", 0.0)) < min_p2:
        return PremiumRiskDecision(True, f"{prefix}_p2_floor", f"P(total>=2)={g.get('p_ge_2_goals', 0.0):.1%}<{min_p2:.0%}")
    if float(g.get("weakest_goal_lambda", 0.0)) < min_weak:
        return PremiumRiskDecision(True, f"{prefix}_weak_goal_lambda", f"weakest goal lambda={g.get('weakest_goal_lambda', 0.0):.2f}<{min_weak:.2f}")

    if m["min_role_last5_ge2"] < min_role5:
        return PremiumRiskDecision(True, f"{prefix}_role_last5_low_total", f"weakest role cleared 1.5 goals only {m['min_role_last5_ge2']}/5<{min_role5}/5")
    if m["min_overall_last5_ge2"] < min_overall5:
        return PremiumRiskDecision(True, f"{prefix}_overall_last5_low_total", f"weakest overall cleared 1.5 goals only {m['min_overall_last5_ge2']}/5<{min_overall5}/5")
    if m["min_role_last3_ge2"] < min_role3:
        return PremiumRiskDecision(True, f"{prefix}_role_recent_one_goal", f"weakest role last3 with 2+ goals={m['min_role_last3_ge2']}/3<{min_role3}/3")
    if m["min_overall_last3_ge2"] < min_overall3:
        return PremiumRiskDecision(True, f"{prefix}_overall_recent_one_goal", f"weakest overall last3 with 2+ goals={m['min_overall_last3_ge2']}/3<{min_overall3}/3")
    if m["min_role_avg_total"] < min_role_avg:
        return PremiumRiskDecision(True, f"{prefix}_role_total_average", f"weakest role avg total={m['min_role_avg_total']:.2f}<{min_role_avg:.2f}")
    if m["min_overall_avg_total"] < min_overall_avg:
        return PremiumRiskDecision(True, f"{prefix}_overall_total_average", f"weakest overall avg total={m['min_overall_avg_total']:.2f}<{min_overall_avg:.2f}")

    return None


def anti_zero_decision_v2912(prediction):
    return _decision(prediction, tier_a=False)


def tier_a_decision_v2912(prediction):
    return _decision(prediction, tier_a=True)


def premium_one_safe_v2912(prediction) -> bool:
    return tier_a_decision_v2912(prediction) is None


def install_btts_v2912_policy() -> None:
    """Install V2.9.12 as the final production BTTS precision layer."""
    from . import btts_v25_policy
    from .premium_risk_guard import PremiumRiskGuard

    if getattr(PremiumRiskGuard, "_btts_v2912_installed", False):
        return
    btts_v25_policy.anti_zero_decision = anti_zero_decision_v2912
    btts_v25_policy.tier_a_decision = tier_a_decision_v2912
    btts_v25_policy.premium_one_safe = premium_one_safe_v2912
    PremiumRiskGuard._btts_v2912_installed = True
