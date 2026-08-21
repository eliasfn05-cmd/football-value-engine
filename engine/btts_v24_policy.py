from __future__ import annotations

"""BTTS V2.4 venue-scoring helpers.

This module intentionally has NO runtime monkey-patching.  Earlier V2.4 hooked
PremiumRiskGuard/DailyPremiumSelector at import time, which made dashboard
rendering fragile.  The authoritative V2.3 publication layer calls these
helpers explicitly instead.
"""

from math import exp

BTTS_V24_ROLE_SAMPLE = 10
BTTS_V24_MIN_ROLE_SAMPLE_HARD = 5
BTTS_V24_ZERO_GOALS_MIN_SAMPLE = 2
BTTS_V24_MIN_SCORING_RATE = 0.40
BTTS_V24_MIN_AVG_GF = 0.65
BTTS_V24_MIN_ROLE_BTTS = 0.35
BTTS_V24_MAX_CONSECUTIVE_SCORELESS = 2
BTTS_V24_MIN_SCORE_PROB = 0.57


def _role_profile(team, fixture, role: str) -> dict | None:
    from .competition_quality import classify_competition
    from .models import Fixture

    if role == "home":
        qs = Fixture.objects.filter(
            home_team=team,
            kickoff__lt=fixture.kickoff,
            home_goals__isnull=False,
            away_goals__isnull=False,
        )
    else:
        qs = Fixture.objects.filter(
            away_team=team,
            kickoff__lt=fixture.kickoff,
            home_goals__isnull=False,
            away_goals__isnull=False,
        )

    qs = qs.select_related("competition_ref", "home_team", "away_team").order_by("-kickoff")
    gf, ga = [], []
    for previous in qs.iterator(chunk_size=50):
        if classify_competition(previous).excluded:
            continue
        if role == "home":
            goals_for = int(previous.home_goals or 0)
            goals_against = int(previous.away_goals or 0)
        else:
            goals_for = int(previous.away_goals or 0)
            goals_against = int(previous.home_goals or 0)
        gf.append(goals_for)
        ga.append(goals_against)
        if len(gf) >= BTTS_V24_ROLE_SAMPLE:
            break

    if not gf:
        return None

    n = len(gf)
    score_rate = sum(v > 0 for v in gf) / n
    avg_gf = sum(gf) / n
    btts_rate = sum(a > 0 and b > 0 for a, b in zip(gf, ga)) / n
    consecutive_scoreless = 0
    for value in gf:  # newest first
        if value != 0:
            break
        consecutive_scoreless += 1

    poisson_score_prob = 1.0 - exp(-max(0.0, avg_gf))
    score_probability = 0.65 * score_rate + 0.35 * poisson_score_prob

    return {
        "n": n,
        "goals": sum(gf),
        "avg_gf": avg_gf,
        "score_rate": score_rate,
        "btts_rate": btts_rate,
        "consecutive_scoreless": consecutive_scoreless,
        "score_probability": score_probability,
    }


def venue_scoring_decision(prediction):
    """Return a PremiumRiskDecision when venue scoring is too weak.

    Fail-open on data/query errors so dashboard rendering can never 500 because
    this optional evidence layer is unavailable.  Core selector/risk gates still
    protect publication in that case.
    """
    from .premium_risk_guard import PremiumRiskDecision

    try:
        fixture = getattr(prediction, "fixture", None)
        if fixture is None:
            return None

        home = _role_profile(fixture.home_team, fixture, "home")
        away = _role_profile(fixture.away_team, fixture, "away")
        if not home or not away:
            return None

        for side, profile in (("home", home), ("away", away)):
            n = profile["n"]
            if n >= BTTS_V24_ZERO_GOALS_MIN_SAMPLE and profile["goals"] == 0:
                return PremiumRiskDecision(True, f"{side}_scoring_gate_zero_goals", f"{side} venue: 0 goals in {n} matches")
            if profile["consecutive_scoreless"] >= BTTS_V24_MAX_CONSECUTIVE_SCORELESS:
                return PremiumRiskDecision(True, f"{side}_scoring_gate_consecutive_blanks", f"{side} venue: {profile['consecutive_scoreless']} consecutive scoreless matches")
            if n >= BTTS_V24_MIN_ROLE_SAMPLE_HARD and profile["score_rate"] < BTTS_V24_MIN_SCORING_RATE:
                return PremiumRiskDecision(True, f"{side}_scoring_gate_low_score_rate", f"{side} venue scoring rate={profile['score_rate']:.0%}<40% (n={n})")
            if n >= BTTS_V24_MIN_ROLE_SAMPLE_HARD and profile["avg_gf"] < BTTS_V24_MIN_AVG_GF:
                return PremiumRiskDecision(True, f"{side}_scoring_gate_low_avg_gf", f"{side} venue avgGF={profile['avg_gf']:.2f}<0.65 (n={n})")
            if n >= BTTS_V24_MIN_ROLE_SAMPLE_HARD and profile["btts_rate"] < BTTS_V24_MIN_ROLE_BTTS:
                return PremiumRiskDecision(True, f"{side}_scoring_gate_low_btts", f"{side} venue BTTS={profile['btts_rate']:.0%}<35% (n={n})")
            if n >= BTTS_V24_MIN_ROLE_SAMPLE_HARD and profile["score_probability"] < BTTS_V24_MIN_SCORE_PROB:
                return PremiumRiskDecision(True, f"{side}_weakest_attack_gate", f"{side} P(score)={profile['score_probability']:.1%}<57%")
        return None
    except Exception:
        return None


def install_btts_v24_policy() -> None:
    """Compatibility no-op.

    V2.4 is enforced explicitly by the V2.3 authoritative publication layer.
    Keeping this function avoids import/startup breakage during rolling deploys.
    """
    return None
