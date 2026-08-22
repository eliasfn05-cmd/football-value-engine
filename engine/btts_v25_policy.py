from __future__ import annotations

"""BTTS V2.5 anti-zero and sample-robustness policy.

The market fails whenever either team finishes on zero. V2.5 therefore
scores the two attacks independently in the exact venue role, shrinks small
samples toward a conservative prior, detects outlier-driven averages and
exposes an explicit safety score for Premium ordering.

Patch 21/08/2026: the 0/3 audit showed that venue-only scoring strength could
still promote teams whose *overall recent bilateral BTTS behaviour* was weak.
We now require recent all-venue scoring/BTTS confirmation, an absolute
calibrated-probability floor and a consensus probability that is constrained
by the weakest scoring side. This is designed to turn weak days into NO BET
instead of forcing three selections.

This module is side-effect free. The authoritative V2.3 publication layer
calls it explicitly so startup/dashboard rendering remains stable.
"""

from math import exp
from statistics import median

ROLE_WINDOW = 10
OVERALL_WINDOW = 10
MIN_ROLE_SAMPLE = 5
MIN_OVERALL_SAMPLE = 5
PRIOR_SCORE_RATE = 0.62
PRIOR_STRENGTH = 5.0

# Premium hard floors (both sides must independently be credible scorers).
PREMIUM_MIN_SCORE_RATE = 0.60
PREMIUM_MIN_AVG_GF = 0.90
PREMIUM_MAX_FTS = 0.40
PREMIUM_MIN_MEDIAN_GF = 1.0
PREMIUM_MAX_ZERO_RISK = 0.35
PREMIUM_MIN_LAST5_SCORED = 3

# New bilateral/recent consensus floors.
PREMIUM_MIN_OVERALL_SCORE_RATE = 0.60
PREMIUM_MIN_OVERALL_LAST5_SCORED = 3
PREMIUM_MIN_OVERALL_LAST5_BTTS = 3
PREMIUM_MIN_CALIBRATED_PROB = 0.59
PREMIUM_MIN_CONSENSUS_PROB = 0.57

# Premium A / #1 safety floors.
TIER_A_MIN_SCORE_RATE = 0.70
TIER_A_MIN_AVG_GF = 1.10
TIER_A_MAX_FTS = 0.30
TIER_A_MIN_BTTS_RATE = 0.55
TIER_A_MIN_LAST5_SCORED = 4
TIER_A_MIN_LAST10_SCORED = 7
TIER_A_MAX_ZERO_RISK = 0.25
TIER_A_MIN_OVERALL_SCORE_RATE = 0.70
TIER_A_MIN_OVERALL_LAST5_SCORED = 4
TIER_A_MIN_OVERALL_LAST5_BTTS = 4
TIER_A_MIN_CALIBRATED_PROB = 0.62
TIER_A_MIN_CONSENSUS_PROB = 0.61
PREMIUM_ONE_MAX_ZERO_RISK = 0.20

# If one extreme game is carrying the attack, it is not Premium A evidence.
OUTLIER_MAX_AVG_GF_DROP = 0.20
OUTLIER_MAX_SCORE_POINTS_DROP = 10.0


def _profile_from_goals(gf: list[int], ga: list[int], current_season_n: int = 0) -> dict | None:
    if not gf:
        return None

    n = len(gf)
    scored = [int(v > 0) for v in gf]
    score_rate = sum(scored) / n
    avg_gf = sum(gf) / n
    med_gf = float(median(gf))
    fts = 1.0 - score_rate
    btts_flags = [int(a > 0 and b > 0) for a, b in zip(gf, ga)]
    btts_rate = sum(btts_flags) / n
    last5_scored = sum(scored[:5])
    last10_scored = sum(scored)
    last5_btts = sum(btts_flags[:5])
    last10_btts = sum(btts_flags)

    robust_gf = list(gf)
    if len(robust_gf) >= 3:
        robust_gf.remove(max(robust_gf))
    robust_avg_gf = sum(robust_gf) / len(robust_gf) if robust_gf else avg_gf
    avg_drop = 0.0 if avg_gf <= 0 else max(0.0, (avg_gf - robust_avg_gf) / avg_gf)

    weight = n / (n + PRIOR_STRENGTH)
    shrunk_score_rate = weight * score_rate + (1.0 - weight) * PRIOR_SCORE_RATE
    poisson_score_prob = 1.0 - exp(-max(0.0, robust_avg_gf))
    score_probability = 0.70 * shrunk_score_rate + 0.30 * poisson_score_prob
    zero_risk = 1.0 - score_probability

    raw_attack_score = 100.0 * (0.65 * score_rate + 0.35 * (1.0 - exp(-max(0.0, avg_gf))))
    robust_attack_score = 100.0 * (0.65 * score_rate + 0.35 * poisson_score_prob)
    outlier_score_drop = max(0.0, raw_attack_score - robust_attack_score)

    return {
        "n": n,
        "current_season_n": current_season_n,
        "goals": sum(gf),
        "avg_gf": avg_gf,
        "robust_avg_gf": robust_avg_gf,
        "median_gf": med_gf,
        "score_rate": score_rate,
        "shrunk_score_rate": shrunk_score_rate,
        "failed_to_score_rate": fts,
        "btts_rate": btts_rate,
        "last5_scored": last5_scored,
        "last10_scored": last10_scored,
        "last5_btts": last5_btts,
        "last10_btts": last10_btts,
        "score_probability": score_probability,
        "zero_risk": zero_risk,
        "outlier_avg_drop": avg_drop,
        "outlier_score_drop": outlier_score_drop,
    }


def _role_profile(team, fixture, role: str) -> dict | None:
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
    gf: list[int] = []
    ga: list[int] = []
    current_season_n = 0

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
        if getattr(previous, "season", None) == getattr(fixture, "season", None):
            current_season_n += 1
        if len(gf) >= ROLE_WINDOW:
            break

    return _profile_from_goals(gf, ga, current_season_n)


def _overall_profile(team, fixture) -> dict | None:
    """Last matches in either venue, used as a recency/BTTS confirmation layer."""
    from django.db.models import Q
    from .competition_quality import classify_competition
    from .models import Fixture

    qs = Fixture.objects.filter(
        Q(home_team=team) | Q(away_team=team),
        kickoff__lt=fixture.kickoff,
        home_goals__isnull=False,
        away_goals__isnull=False,
    ).select_related("competition_ref", "home_team", "away_team").order_by("-kickoff")

    gf: list[int] = []
    ga: list[int] = []
    current_season_n = 0
    for previous in qs.iterator(chunk_size=50):
        if classify_competition(previous).excluded:
            continue
        if previous.home_team_id == team.id:
            goals_for = int(previous.home_goals or 0)
            goals_against = int(previous.away_goals or 0)
        else:
            goals_for = int(previous.away_goals or 0)
            goals_against = int(previous.home_goals or 0)
        gf.append(goals_for)
        ga.append(goals_against)
        if getattr(previous, "season", None) == getattr(fixture, "season", None):
            current_season_n += 1
        if len(gf) >= OVERALL_WINDOW:
            break

    return _profile_from_goals(gf, ga, current_season_n)


def anti_zero_metrics(prediction) -> dict:
    fixture = getattr(prediction, "fixture", None)
    if fixture is None:
        return {"available": False}
    try:
        home = _role_profile(fixture.home_team, fixture, "home")
        away = _role_profile(fixture.away_team, fixture, "away")
        home_overall = _overall_profile(fixture.home_team, fixture)
        away_overall = _overall_profile(fixture.away_team, fixture)
    except Exception:
        return {"available": False}
    if not home or not away or not home_overall or not away_overall:
        return {"available": False}

    weakest_score_probability = min(home["score_probability"], away["score_probability"])
    max_zero_risk = max(home["zero_risk"], away["zero_risk"])
    sample_confidence = min(1.0, min(home["n"], away["n"]) / ROLE_WINDOW)
    overall_sample_confidence = min(1.0, min(home_overall["n"], away_overall["n"]) / OVERALL_WINDOW)

    anti_zero_component = max(0.0, 1.0 - max_zero_risk)
    try:
        from .probability_calibration import ProbabilityEVCalibrationService
        calibration = ProbabilityEVCalibrationService().calibrate(prediction)
        p_btts = float(calibration.calibrated_probability or 0.0)
        implied = float(calibration.implied_probability or 0.0)
        ev = max(0.0, min(float(calibration.reliable_ev or 0.0) / 0.20, 1.0))
    except Exception:
        p_btts = float(getattr(prediction, "probability", 0.0) or 0.0)
        implied = 0.0
        ev = 0.0

    # Empirical bilateral confirmation: both teams must repeatedly participate
    # in BTTS, not merely have one strong attack each in isolated venue splits.
    empirical_btts = min(
        (home["btts_rate"] + home_overall["btts_rate"]) / 2.0,
        (away["btts_rate"] + away_overall["btts_rate"]) / 2.0,
    )

    # Consensus probability deliberately cannot exceed the weakest scoring side
    # by much. It combines calibrated model probability, weakest-team scoring
    # probability and empirical BTTS participation.
    consensus_probability = (
        0.45 * p_btts
        + 0.35 * weakest_score_probability
        + 0.20 * empirical_btts
    )
    consensus_probability = min(consensus_probability, weakest_score_probability + 0.05)

    safety_score = 100.0 * (
        0.30 * consensus_probability
        + 0.25 * anti_zero_component
        + 0.15 * empirical_btts
        + 0.10 * sample_confidence
        + 0.10 * overall_sample_confidence
        + 0.10 * ev
    )

    return {
        "available": True,
        "home": home,
        "away": away,
        "home_overall": home_overall,
        "away_overall": away_overall,
        "weakest_score_probability": weakest_score_probability,
        "max_zero_risk": max_zero_risk,
        "sample_confidence": sample_confidence,
        "overall_sample_confidence": overall_sample_confidence,
        "calibrated_probability": p_btts,
        "market_implied_probability": implied,
        "empirical_btts": empirical_btts,
        "consensus_probability": consensus_probability,
        "safety_score": round(safety_score, 2),
    }


def anti_zero_decision(prediction):
    """Hard Premium gate against likely 0-x / x-0 outcomes."""
    from .premium_risk_guard import PremiumRiskDecision

    metrics = anti_zero_metrics(prediction)
    if not metrics.get("available"):
        return None

    for side in ("home", "away"):
        p = metrics[side]
        if p["n"] < MIN_ROLE_SAMPLE:
            return PremiumRiskDecision(True, f"{side}_v25_sample_incomplete", f"{side} role sample n={p['n']}<5")
        if p["score_rate"] < PREMIUM_MIN_SCORE_RATE:
            return PremiumRiskDecision(True, f"{side}_v25_low_score_rate", f"{side} scores {p['score_rate']:.0%}<60%")
        if p["avg_gf"] < PREMIUM_MIN_AVG_GF:
            return PremiumRiskDecision(True, f"{side}_v25_low_avg_gf", f"{side} avgGF={p['avg_gf']:.2f}<0.90")
        if p["failed_to_score_rate"] > PREMIUM_MAX_FTS:
            return PremiumRiskDecision(True, f"{side}_v25_fts", f"{side} FTS={p['failed_to_score_rate']:.0%}>40%")
        if p["median_gf"] < PREMIUM_MIN_MEDIAN_GF:
            return PremiumRiskDecision(True, f"{side}_v25_low_median", f"{side} medianGF={p['median_gf']:.1f}<1.0")
        if p["last5_scored"] < PREMIUM_MIN_LAST5_SCORED:
            return PremiumRiskDecision(True, f"{side}_v25_recent_blanks", f"{side} scored {p['last5_scored']}/5")
        if p["zero_risk"] > PREMIUM_MAX_ZERO_RISK:
            return PremiumRiskDecision(True, f"{side}_v25_zero_risk", f"{side} zero-risk={p['zero_risk']:.1%}>35%")

        overall = metrics[f"{side}_overall"]
        if overall["n"] < MIN_OVERALL_SAMPLE:
            return PremiumRiskDecision(True, f"{side}_v25_overall_sample", f"{side} overall sample n={overall['n']}<5")
        if overall["score_rate"] < PREMIUM_MIN_OVERALL_SCORE_RATE:
            return PremiumRiskDecision(True, f"{side}_v25_overall_score_rate", f"{side} overall scores {overall['score_rate']:.0%}<60%")
        if overall["last5_scored"] < PREMIUM_MIN_OVERALL_LAST5_SCORED:
            return PremiumRiskDecision(True, f"{side}_v25_overall_recent_blanks", f"{side} overall scored {overall['last5_scored']}/5")
        if overall["last5_btts"] < PREMIUM_MIN_OVERALL_LAST5_BTTS:
            return PremiumRiskDecision(True, f"{side}_v25_recent_btts", f"{side} BTTS participation {overall['last5_btts']}/5<3")

    if metrics["calibrated_probability"] < PREMIUM_MIN_CALIBRATED_PROB:
        return PremiumRiskDecision(
            True,
            "v25_calibrated_probability_floor",
            f"calibrated BTTS={metrics['calibrated_probability']:.1%}<59%",
        )
    if metrics["consensus_probability"] < PREMIUM_MIN_CONSENSUS_PROB:
        return PremiumRiskDecision(
            True,
            "v25_consensus_probability_floor",
            f"consensus BTTS={metrics['consensus_probability']:.1%}<57%",
        )
    return None


def tier_a_decision(prediction):
    """Extra evidence required before a candidate may behave as Premium A."""
    from .premium_risk_guard import PremiumRiskDecision

    metrics = anti_zero_metrics(prediction)
    if not metrics.get("available"):
        return PremiumRiskDecision(True, "v25_tier_a_evidence_missing", "anti-zero evidence unavailable")

    for side in ("home", "away"):
        p = metrics[side]
        if p["score_rate"] < TIER_A_MIN_SCORE_RATE:
            return PremiumRiskDecision(True, f"{side}_v25_a_score_rate", f"{side} scores {p['score_rate']:.0%}<70%")
        if p["avg_gf"] < TIER_A_MIN_AVG_GF:
            return PremiumRiskDecision(True, f"{side}_v25_a_avg_gf", f"{side} avgGF={p['avg_gf']:.2f}<1.10")
        if p["failed_to_score_rate"] > TIER_A_MAX_FTS:
            return PremiumRiskDecision(True, f"{side}_v25_a_fts", f"{side} FTS={p['failed_to_score_rate']:.0%}>30%")
        if p["btts_rate"] < TIER_A_MIN_BTTS_RATE:
            return PremiumRiskDecision(True, f"{side}_v25_a_btts", f"{side} venue BTTS={p['btts_rate']:.0%}<55%")
        if p["last5_scored"] < TIER_A_MIN_LAST5_SCORED:
            return PremiumRiskDecision(True, f"{side}_v25_a_last5", f"{side} scored {p['last5_scored']}/5<4")
        if p["n"] >= 10 and p["last10_scored"] < TIER_A_MIN_LAST10_SCORED:
            return PremiumRiskDecision(True, f"{side}_v25_a_last10", f"{side} scored {p['last10_scored']}/10<7")
        if p["zero_risk"] > TIER_A_MAX_ZERO_RISK:
            return PremiumRiskDecision(True, f"{side}_v25_a_zero_risk", f"{side} zero-risk={p['zero_risk']:.1%}>25%")
        if p["outlier_avg_drop"] > OUTLIER_MAX_AVG_GF_DROP or p["outlier_score_drop"] > OUTLIER_MAX_SCORE_POINTS_DROP:
            return PremiumRiskDecision(
                True,
                f"{side}_v25_outlier_dependency",
                f"{side} outlier avg drop={p['outlier_avg_drop']:.0%}, score drop={p['outlier_score_drop']:.1f}",
            )

        overall = metrics[f"{side}_overall"]
        if overall["score_rate"] < TIER_A_MIN_OVERALL_SCORE_RATE:
            return PremiumRiskDecision(True, f"{side}_v25_a_overall_score", f"{side} overall scores {overall['score_rate']:.0%}<70%")
        if overall["last5_scored"] < TIER_A_MIN_OVERALL_LAST5_SCORED:
            return PremiumRiskDecision(True, f"{side}_v25_a_overall_last5", f"{side} overall scored {overall['last5_scored']}/5<4")
        if overall["last5_btts"] < TIER_A_MIN_OVERALL_LAST5_BTTS:
            return PremiumRiskDecision(True, f"{side}_v25_a_recent_btts", f"{side} BTTS participation {overall['last5_btts']}/5<4")

    if metrics["calibrated_probability"] < TIER_A_MIN_CALIBRATED_PROB:
        return PremiumRiskDecision(
            True,
            "v25_a_calibrated_probability_floor",
            f"calibrated BTTS={metrics['calibrated_probability']:.1%}<62%",
        )
    if metrics["consensus_probability"] < TIER_A_MIN_CONSENSUS_PROB:
        return PremiumRiskDecision(
            True,
            "v25_a_consensus_probability_floor",
            f"consensus BTTS={metrics['consensus_probability']:.1%}<61%",
        )
    return None


def premium_one_safe(prediction) -> bool:
    metrics = anti_zero_metrics(prediction)
    return bool(
        metrics.get("available")
        and metrics["max_zero_risk"] <= PREMIUM_ONE_MAX_ZERO_RISK
        and metrics["calibrated_probability"] >= TIER_A_MIN_CALIBRATED_PROB
        and metrics["consensus_probability"] >= TIER_A_MIN_CONSENSUS_PROB
        and min(metrics["home_overall"]["last5_btts"], metrics["away_overall"]["last5_btts"])
        >= TIER_A_MIN_OVERALL_LAST5_BTTS
    )


def premium_safety_score(prediction) -> float:
    metrics = anti_zero_metrics(prediction)
    return float(metrics.get("safety_score") or 0.0)


def install_btts_v25_policy() -> None:
    """Compatibility no-op; V2.3 invokes this module explicitly."""
    return None
