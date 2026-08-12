from __future__ import annotations

from dataclasses import dataclass

from .models import Prediction


@dataclass(frozen=True)
class PremiumRiskDecision:
    blocked: bool
    code: str = ""
    detail: str = ""


class PremiumRiskGuard:
    """Hard loss-prevention guards learned from Premium backtesting.

    Sprint 7.10 deliberately gives the last five venue-specific matches more
    authority than broad form, model probability or apparent EV. These guards
    are only used for final Premium admission; they do not alter the raw model.
    """

    RECENT_N = 5
    OVER25_MIN_RECENT_SIDE = 0.50
    BTTS_MIN_RECENT_SIDE = 0.50
    BTTS_MAX_RECENT_FTS = 0.40
    BTTS_STRONG_CLEAN_SHEET = 0.50

    @staticmethod
    def _float(evidence: dict, key: str, default=None):
        try:
            value = evidence.get(key, default)
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    @classmethod
    def evaluate(cls, prediction: Prediction) -> PremiumRiskDecision:
        evidence = (prediction.reasons or {}).get("deep_analysis_evidence") or {}
        try:
            home_n = int(evidence.get("home_recent_n") or 0)
            away_n = int(evidence.get("away_recent_n") or 0)
        except (TypeError, ValueError):
            home_n = away_n = 0

        # Do not invent a veto without the requested venue sample. The existing
        # Deep/reliability gates remain responsible for insufficient coverage.
        if home_n < cls.RECENT_N or away_n < cls.RECENT_N:
            return PremiumRiskDecision(False)

        if prediction.market == "OVER_2_5":
            home_recent = cls._float(evidence, "home_recent_over25_rate")
            away_recent = cls._float(evidence, "away_recent_over25_rate")
            if home_recent is None or away_recent is None:
                return PremiumRiskDecision(False)

            weak_side = "home" if home_recent <= away_recent else "away"
            weak_rate = min(home_recent, away_recent)
            if weak_rate < cls.OVER25_MIN_RECENT_SIDE:
                return PremiumRiskDecision(
                    True,
                    "venue_recent_over25_hard_floor",
                    f"{weak_side} recent venue Over2.5 {weak_rate:.0%} < {cls.OVER25_MIN_RECENT_SIDE:.0%}",
                )
            return PremiumRiskDecision(False)

        if prediction.market == "BTTS":
            home_recent = cls._float(evidence, "home_recent_btts_rate")
            away_recent = cls._float(evidence, "away_recent_btts_rate")
            home_fts = cls._float(evidence, "home_recent_failed_to_score_rate", 0.0)
            away_fts = cls._float(evidence, "away_recent_failed_to_score_rate", 0.0)
            home_cs = cls._float(evidence, "home_clean_sheet_rate", 0.0)
            away_cs = cls._float(evidence, "away_clean_sheet_rate", 0.0)
            if home_recent is None or away_recent is None:
                return PremiumRiskDecision(False)

            weak_side = "home" if home_recent <= away_recent else "away"
            weak_rate = min(home_recent, away_recent)
            if weak_rate < cls.BTTS_MIN_RECENT_SIDE:
                return PremiumRiskDecision(
                    True,
                    "venue_recent_btts_hard_floor",
                    f"{weak_side} recent venue BTTS {weak_rate:.0%} < {cls.BTTS_MIN_RECENT_SIDE:.0%}",
                )

            # A team failing to score in 2+ of its last 5 venue-specific games is
            # too fragile for a Premium BTTS position, even if the opposite side
            # and the market create attractive aggregate probability/EV.
            if home_fts >= cls.BTTS_MAX_RECENT_FTS:
                return PremiumRiskDecision(
                    True,
                    "home_recent_scoring_fragility",
                    f"home recent failed-to-score {home_fts:.0%} >= {cls.BTTS_MAX_RECENT_FTS:.0%}",
                )
            if away_fts >= cls.BTTS_MAX_RECENT_FTS:
                return PremiumRiskDecision(
                    True,
                    "away_recent_scoring_fragility",
                    f"away recent failed-to-score {away_fts:.0%} >= {cls.BTTS_MAX_RECENT_FTS:.0%}",
                )

            # Compound nil-risk guard: combine scoring fragility with the
            # opponent's venue clean-sheet history. This targets 0-0/1-0 type
            # failures without blindly penalizing every strong defence.
            if home_fts >= 0.20 and away_cs >= cls.BTTS_STRONG_CLEAN_SHEET:
                return PremiumRiskDecision(
                    True,
                    "btts_nil_risk_home",
                    f"home FTS {home_fts:.0%} + away clean sheets {away_cs:.0%}",
                )
            if away_fts >= 0.20 and home_cs >= cls.BTTS_STRONG_CLEAN_SHEET:
                return PremiumRiskDecision(
                    True,
                    "btts_nil_risk_away",
                    f"away FTS {away_fts:.0%} + home clean sheets {home_cs:.0%}",
                )

        return PremiumRiskDecision(False)
