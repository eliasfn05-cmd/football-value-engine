from __future__ import annotations

"""BTTS V2 runtime policy.

The first BTTS-only iteration accumulated too many independent hard gates.  This
module keeps genuinely dangerous contradictions as vetoes, but turns ordinary
BTTS/venue/H2H variation into ranking/reliability penalties.  Missing H2H is
neutral evidence, never a reason to reject a candidate.
"""


def install_btts_v2_policy() -> None:
    from .btts_h2h_guard import h2h_metrics
    from .premium_risk_guard import PremiumRiskDecision, PremiumRiskGuard
    from .premium_selection import DailyPremiumSelector

    if getattr(PremiumRiskGuard, "_btts_v2_installed", False):
        return

    # ------------------------------------------------------------------
    # 1) Risk guard: only EXTREME scoring/defensive profiles are hard vetoes.
    #    Moderate evidence is left to probability calibration + ranking.
    # ------------------------------------------------------------------
    PremiumRiskGuard.BTTS_MIN_RECENT_SIDE = 0.20
    PremiumRiskGuard.BTTS_MIN_LONG_SIDE = 0.25
    PremiumRiskGuard.BTTS_MIN_RECENT_COMBINED = 0.45
    PremiumRiskGuard.BTTS_MAX_RECENT_FTS = 0.60
    PremiumRiskGuard.BTTS_STRONG_CLEAN_SHEET = 0.70

    PremiumRiskGuard.BTTS_CURRENT_ATTACK_MIN_AVG_GF = 0.60
    PremiumRiskGuard.BTTS_CURRENT_ATTACK_MAX_FTS = 0.60

    PremiumRiskGuard.BTTS_DEFENSIVE_SUPPRESSION_CS = 0.80
    PremiumRiskGuard.BTTS_DEFENSIVE_SUPPRESSION_OPP_GF = 0.90
    PremiumRiskGuard.BTTS_DEFENSIVE_SUPPRESSION_MAX_OPP_CONCEDED = 0.80

    PremiumRiskGuard.BTTS_LOW_EVENT_MAX_COMBINED_AVG_TOTAL = 1.60
    PremiumRiskGuard.BTTS_LOW_EVENT_MAX_RECENT_OVER25 = 0.20

    @classmethod
    def h2h_risk_v2(
        cls,
        prediction,
        *,
        h_recent: float,
        a_recent: float,
        h_long: float,
        a_long: float,
        h_fts: float,
        a_fts: float,
    ):
        metrics = h2h_metrics(prediction)
        sample = int(metrics.get("sample") or 0)
        if sample < 5:
            # Absence/short H2H history is unknown information, not negative
            # evidence.  It must never create a Premium veto by itself.
            return None

        rate = float(metrics.get("btts_rate") or 0.0)
        recent3_all_no = bool(metrics.get("recent3_all_no"))
        if rate <= 0.20 and recent3_all_no:
            return PremiumRiskDecision(
                True,
                "btts_h2h_extreme_contradiction",
                f"H2H n={sample}, BTTS={rate:.0%}, last3=NO",
            )
        return None

    PremiumRiskGuard._h2h_risk = h2h_risk_v2

    # ------------------------------------------------------------------
    # 2) Venue history: make normal 40-60% BTTS variation a SOFT signal.
    #    Hard-block only when the weak side is genuinely unable to score.
    # ------------------------------------------------------------------
    @classmethod
    def venue_metrics_v2(cls, prediction):
        if prediction.market != "BTTS":
            return {
                "blocked": True,
                "severity": 1.0,
                "reliability_penalty": 0.06,
                "rank_penalty": 7.0,
                "weak_side": None,
                "reason": "btts_only_market",
                "policy": "BTTS_V2",
            }

        evidence = (prediction.reasons or {}).get("deep_analysis_evidence") or {}
        try:
            home_n = int(evidence.get("home_recent_n") or 0)
            away_n = int(evidence.get("away_recent_n") or 0)
            home_long = float(evidence.get("home_btts_rate"))
            away_long = float(evidence.get("away_btts_rate"))
            home_recent = float(evidence.get("home_recent_btts_rate"))
            away_recent = float(evidence.get("away_recent_btts_rate"))
        except (TypeError, ValueError):
            return {
                "blocked": True,
                "severity": 1.0,
                "reliability_penalty": 0.06,
                "rank_penalty": 7.0,
                "weak_side": None,
                "reason": "btts_venue_evidence_missing",
                "policy": "BTTS_V2",
            }

        # A short venue sample reduces confidence but does not kill a candidate.
        sample_factor = min(1.0, min(home_n, away_n) / 5.0) if min(home_n, away_n) > 0 else 0.0

        if home_recent <= away_recent:
            weak_side = "home"
            weak_recent, weak_long = home_recent, home_long
        else:
            weak_side = "away"
            weak_recent, weak_long = away_recent, away_long

        try:
            weak_fts = float(
                evidence.get(
                    "home_recent_failed_to_score_rate"
                    if weak_side == "home"
                    else "away_recent_failed_to_score_rate"
                )
                or 0.0
            )
        except (TypeError, ValueError):
            weak_fts = 0.0

        # Continuous penalty: below a 55% venue BTTS rate gradually loses rank,
        # but 50% is not treated as a failure.  Long-run history contributes a
        # smaller secondary penalty.
        recent_shortfall = max(0.0, 0.55 - weak_recent) / 0.55
        long_shortfall = max(0.0, 0.45 - weak_long) / 0.45
        sample_penalty = (1.0 - sample_factor) * 0.25
        severity = min(1.0, 0.65 * recent_shortfall + 0.20 * long_shortfall + sample_penalty)

        reliability_penalty = severity * 0.045
        rank_penalty = severity * 5.0

        # True hard veto: almost no BTTS evidence AND repeated failure to score.
        blocked = weak_recent <= 0.20 and weak_fts >= 0.60 and min(home_n, away_n) >= 5
        reason = "extreme_venue_scoring_failure" if blocked else None

        return {
            "blocked": blocked,
            "severity": round(severity, 4),
            "reliability_penalty": round(reliability_penalty, 4),
            "rank_penalty": round(rank_penalty, 2),
            "weak_side": weak_side,
            "weak_recent_rate": round(weak_recent, 3),
            "weak_long_rate": round(weak_long, 3),
            "weak_failed_to_score_rate": round(weak_fts, 3),
            "sample_factor": round(sample_factor, 3),
            "reason": reason,
            "policy": "BTTS_V2",
        }

    DailyPremiumSelector._venue_contradiction_metrics = venue_metrics_v2

    # Keep positive-value discipline.  We are recovering recall by removing
    # redundant vetoes, not by accepting negative-EV or low-quality prices.
    DailyPremiumSelector._btts_v2_installed = True
    PremiumRiskGuard._btts_v2_installed = True
