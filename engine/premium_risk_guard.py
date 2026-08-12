from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Q

from .competition_quality import classify_competition
from .models import Fixture, Prediction, Team


@dataclass(frozen=True)
class PremiumRiskDecision:
    blocked: bool
    code: str = ""
    detail: str = ""


class PremiumRiskGuard:
    """Hard loss-prevention guards learned from Premium backtesting.

    Sprint 7.10 deliberately gives the last five venue-specific matches more
    authority than broad form, model probability or apparent EV. A second,
    narrower current-attack check catches BTTS profiles where recent scoring
    health has deteriorated across all venues.
    """

    RECENT_N = 5
    OVER25_MIN_RECENT_SIDE = 0.50
    BTTS_MIN_RECENT_SIDE = 0.50
    BTTS_MAX_RECENT_FTS = 0.40
    BTTS_STRONG_CLEAN_SHEET = 0.50
    BTTS_CURRENT_ATTACK_MIN_AVG_GF = 0.80
    BTTS_CURRENT_ATTACK_MAX_FTS = 0.40

    @staticmethod
    def _float(evidence: dict, key: str, default=None):
        try:
            value = evidence.get(key, default)
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    @classmethod
    def _current_attack_profile(cls, team: Team, before_fixture: Fixture) -> dict | None:
        fixtures = (
            Fixture.objects.filter(
                kickoff__lt=before_fixture.kickoff,
                home_goals__isnull=False,
                away_goals__isnull=False,
            )
            .filter(Q(home_team=team) | Q(away_team=team))
            .select_related("home_team", "away_team", "competition_ref")
            .order_by("-kickoff")
        )
        goals_for = []
        for fixture in fixtures.iterator(chunk_size=50):
            if classify_competition(fixture).excluded:
                continue
            gf = int(fixture.home_goals or 0) if fixture.home_team_id == team.id else int(fixture.away_goals or 0)
            goals_for.append(gf)
            if len(goals_for) >= cls.RECENT_N:
                break
        if len(goals_for) < cls.RECENT_N:
            return None
        return {
            "n": len(goals_for),
            "avg_goals_for": sum(goals_for) / len(goals_for),
            "failed_to_score_rate": sum(1 for value in goals_for if value == 0) / len(goals_for),
        }

    @classmethod
    def evaluate(cls, prediction: Prediction) -> PremiumRiskDecision:
        evidence = (prediction.reasons or {}).get("deep_analysis_evidence") or {}
        try:
            home_n = int(evidence.get("home_recent_n") or 0)
            away_n = int(evidence.get("away_recent_n") or 0)
        except (TypeError, ValueError):
            home_n = away_n = 0

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

            fixture = getattr(prediction, "fixture", None)
            if fixture is not None:
                for side, team in (("home", fixture.home_team), ("away", fixture.away_team)):
                    profile = cls._current_attack_profile(team, fixture)
                    if profile is None:
                        continue
                    if (
                        profile["avg_goals_for"] < cls.BTTS_CURRENT_ATTACK_MIN_AVG_GF
                        or profile["failed_to_score_rate"] >= cls.BTTS_CURRENT_ATTACK_MAX_FTS
                    ):
                        return PremiumRiskDecision(
                            True,
                            f"{side}_current_attack_drought",
                            f"{side} last5 all-venue avgGF={profile['avg_goals_for']:.2f}, FTS={profile['failed_to_score_rate']:.0%}",
                        )

        return PremiumRiskDecision(False)
