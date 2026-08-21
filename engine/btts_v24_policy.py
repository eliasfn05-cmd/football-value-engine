from __future__ import annotations

"""BTTS V2.4: venue-scoring authority and weakest-attack protection.

The BTTS market requires BOTH teams to demonstrate independent scoring ability.
This policy prevents high-total-goal environments from hiding a venue-specific
attack that is not scoring.  It is installed after V2.3 so it applies to fresh
candidates, rescue candidates, publication locks and dashboard rendering.
"""

from math import exp
from django.db.models import Q

BTTS_V24_ROLE_SAMPLE = 10
BTTS_V24_MIN_ROLE_SAMPLE_HARD = 5
BTTS_V24_ZERO_GOALS_MIN_SAMPLE = 2
BTTS_V24_MIN_SCORING_RATE = 0.40
BTTS_V24_MIN_AVG_GF = 0.65
BTTS_V24_MIN_ROLE_BTTS = 0.35
BTTS_V24_MAX_CONSECUTIVE_SCORELESS = 2
BTTS_V24_MIN_SCORE_PROB = 0.57
BTTS_V24_TIER_A_HOME_SCORE_PROB = 0.72
BTTS_V24_TIER_A_AWAY_SCORE_PROB = 0.62
BTTS_V24_TIER_B_SCORE_PROB = 0.57


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

    # Blend empirical scoring frequency with a Poisson scoring probability.
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
    from .premium_risk_guard import PremiumRiskDecision

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
            return PremiumRiskDecision(
                True,
                f"{side}_scoring_gate_zero_goals",
                f"{side} venue: 0 goals in {n} matches",
            )
        if profile["consecutive_scoreless"] >= BTTS_V24_MAX_CONSECUTIVE_SCORELESS:
            return PremiumRiskDecision(
                True,
                f"{side}_scoring_gate_consecutive_blanks",
                f"{side} venue: {profile['consecutive_scoreless']} consecutive scoreless matches",
            )
        if n >= BTTS_V24_MIN_ROLE_SAMPLE_HARD and profile["score_rate"] < BTTS_V24_MIN_SCORING_RATE:
            return PremiumRiskDecision(
                True,
                f"{side}_scoring_gate_low_score_rate",
                f"{side} venue scoring rate={profile['score_rate']:.0%}<40% (n={n})",
            )
        if n >= BTTS_V24_MIN_ROLE_SAMPLE_HARD and profile["avg_gf"] < BTTS_V24_MIN_AVG_GF:
            return PremiumRiskDecision(
                True,
                f"{side}_scoring_gate_low_avg_gf",
                f"{side} venue avgGF={profile['avg_gf']:.2f}<0.65 (n={n})",
            )
        if n >= BTTS_V24_MIN_ROLE_SAMPLE_HARD and profile["btts_rate"] < BTTS_V24_MIN_ROLE_BTTS:
            return PremiumRiskDecision(
                True,
                f"{side}_scoring_gate_low_btts",
                f"{side} venue BTTS={profile['btts_rate']:.0%}<35% (n={n})",
            )
        if n >= BTTS_V24_MIN_ROLE_SAMPLE_HARD and profile["score_probability"] < BTTS_V24_MIN_SCORE_PROB:
            return PremiumRiskDecision(
                True,
                f"{side}_weakest_attack_gate",
                f"{side} P(score)={profile['score_probability']:.1%}<57%",
            )
    return None


def install_btts_v24_policy() -> None:
    from .premium_risk_guard import PremiumRiskGuard
    from .premium_selection import DailyPremiumSelector

    if getattr(PremiumRiskGuard, "_btts_v24_installed", False):
        return

    original_evaluate = PremiumRiskGuard.evaluate.__func__

    @classmethod
    def evaluate_v24(cls, prediction):
        original = original_evaluate(cls, prediction)
        if original.blocked:
            return original
        venue_decision = venue_scoring_decision(prediction)
        if venue_decision:
            return venue_decision
        return original

    PremiumRiskGuard.evaluate = evaluate_v24
    PremiumRiskGuard._btts_v24_installed = True

    # Rank penalty / tier evidence: even when above hard floors, a weak scoring
    # side cannot be compensated by the opponent's strength or total-goal profile.
    if not getattr(DailyPremiumSelector, "_btts_v24_installed", False):
        original_rank = DailyPremiumSelector._rank_score

        def rank_score_v24(self, prediction):
            rank, rationale = original_rank(self, prediction)
            rationale = dict(rationale or {})
            fixture = prediction.fixture
            home = _role_profile(fixture.home_team, fixture, "home")
            away = _role_profile(fixture.away_team, fixture, "away")
            if home and away:
                weak_p = min(home["score_probability"], away["score_probability"])
                penalty = max(0.0, 0.65 - weak_p) / 0.08 * 4.0
                penalty = min(12.0, penalty)
                rank = max(0.0, float(rank) - penalty)
                rationale["btts_v24_scoring_gate"] = {
                    "home_score_probability": round(home["score_probability"], 4),
                    "away_score_probability": round(away["score_probability"], 4),
                    "home_role_n": home["n"],
                    "away_role_n": away["n"],
                    "weakest_attack_probability": round(weak_p, 4),
                    "rank_penalty": round(penalty, 2),
                    "tier_a_requirements": {
                        "home": BTTS_V24_TIER_A_HOME_SCORE_PROB,
                        "away": BTTS_V24_TIER_A_AWAY_SCORE_PROB,
                    },
                    "tier_b_min_each": BTTS_V24_TIER_B_SCORE_PROB,
                }
            return rank, rationale

        DailyPremiumSelector._rank_score = rank_score_v24
        DailyPremiumSelector._btts_v24_installed = True
