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

    Sprint 7.11 makes Premium admission deliberately asymmetric: broad form,
    model probability and EV may rank a candidate, but the last five
    home-at-home / away-at-away matches have veto authority. Over 2.5 now
    requires a genuine two-sided venue signal plus at least one strong anchor
    side, and adds explicit 0-0/1-0 risk controls. BTTS keeps the Sprint 7.10
    venue and current-attack drought guards.
    """

    RECENT_N = 5

    # Over 2.5: at least 3/5 on BOTH venue sides and at least 4/5 on one side.
    OVER25_MIN_RECENT_SIDE = 0.60
    OVER25_STRONG_ANCHOR_SIDE = 0.80
    OVER25_MIN_RECENT_COMBINED = 0.70
    OVER25_MIN_MARKET_SUPPORT = 0.65
    OVER25_MAX_RECENT_FTS_FOR_NIL_RISK = 0.40
    OVER25_STRONG_CLEAN_SHEET = 0.40

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

        # Premium cannot claim venue certainty without the requested sample.
        # Leave low coverage to the existing Deep/reliability gates rather than
        # inventing rates from smaller samples.
        if home_n < cls.RECENT_N or away_n < cls.RECENT_N:
            return PremiumRiskDecision(False)

        if prediction.market == "OVER_2_5":
            home_recent = cls._float(evidence, "home_recent_over25_rate")
            away_recent = cls._float(evidence, "away_recent_over25_rate")
            home_fts = cls._float(evidence, "home_recent_failed_to_score_rate", 0.0)
            away_fts = cls._float(evidence, "away_recent_failed_to_score_rate", 0.0)
            home_cs = cls._float(evidence, "home_clean_sheet_rate", 0.0)
            away_cs = cls._float(evidence, "away_clean_sheet_rate", 0.0)
            market_support = cls._float(evidence, "market_support_index", 0.0)
            if home_recent is None or away_recent is None:
                return PremiumRiskDecision(False)

            weak_side = "home" if home_recent <= away_recent else "away"
            weak_rate = min(home_recent, away_recent)
            strong_rate = max(home_recent, away_recent)
            combined_recent = (home_recent + away_recent) / 2.0

            # Avaí–CRB lesson: 2/5 (40%) at the relevant venue can never be
            # rescued by the opponent, raw probability, score or apparent EV.
            if weak_rate < cls.OVER25_MIN_RECENT_SIDE:
                return PremiumRiskDecision(
                    True,
                    "venue_recent_over25_hard_floor",
                    f"{weak_side} recent venue Over2.5 {weak_rate:.0%} < {cls.OVER25_MIN_RECENT_SIDE:.0%}",
                )

            # Bremer–Phönix / Tampa–Louisville lesson: 3/5 + 3/5 is still too
            # fragile for Premium. Require one side to be a genuine 4/5 anchor
            # and the combined last-five signal to be at least 70%.
            if strong_rate < cls.OVER25_STRONG_ANCHOR_SIDE:
                return PremiumRiskDecision(
                    True,
                    "over25_no_strong_venue_anchor",
                    f"best venue Over2.5 side {strong_rate:.0%} < {cls.OVER25_STRONG_ANCHOR_SIDE:.0%}",
                )
            if combined_recent < cls.OVER25_MIN_RECENT_COMBINED:
                return PremiumRiskDecision(
                    True,
                    "over25_recent_combined_floor",
                    f"combined recent venue Over2.5 {combined_recent:.0%} < {cls.OVER25_MIN_RECENT_COMBINED:.0%}",
                )
            if market_support < cls.OVER25_MIN_MARKET_SUPPORT:
                return PremiumRiskDecision(
                    True,
                    "over25_market_support_hard_floor",
                    f"Deep market support {market_support:.3f} < {cls.OVER25_MIN_MARKET_SUPPORT:.2f}",
                )

            # Explicit nil-risk guard. Over can survive one team blanking only
            # when the opponent is not simultaneously showing a strong clean-
            # sheet profile. This targets 0-0/1-0/2-0 tails that aggregate goal
            # averages and EV systematically underweight.
            if home_fts >= cls.OVER25_MAX_RECENT_FTS_FOR_NIL_RISK and away_cs >= cls.OVER25_STRONG_CLEAN_SHEET:
                return PremiumRiskDecision(
                    True,
                    "over25_nil_risk_home",
                    f"home recent FTS {home_fts:.0%} + away clean sheets {away_cs:.0%}",
                )
            if away_fts >= cls.OVER25_MAX_RECENT_FTS_FOR_NIL_RISK and home_cs >= cls.OVER25_STRONG_CLEAN_SHEET:
                return PremiumRiskDecision(
                    True,
                    "over25_nil_risk_away",
                    f"away recent FTS {away_fts:.0%} + home clean sheets {home_cs:.0%}",
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
