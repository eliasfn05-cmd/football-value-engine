from __future__ import annotations

from dataclasses import dataclass
from django.db.models import Q

from .competition_quality import classify_competition
from .features import FeatureVector
from .models import Fixture


@dataclass(frozen=True)
class MarketConfidenceResult:
    score: float
    passed: bool
    failures: list[str]
    evidence: dict[str, float | int | None]


class MarketConfidenceService:
    """Validate that a betting market is supported by persisted context.

    No external API calls are made here. Venue evidence comes from V8 features;
    H2H and competition tendencies are computed from finished official matches
    already stored in the database.

    Performance note: OVER_2_5 and BTTS for the same fixture consume exactly the
    same H2H/competition history. Cache that immutable evidence inside the service
    so a V8 evaluation performs each database lookup once instead of once per
    market. This changes no scoring or Premium rule.
    """

    min_confidence = 50.0

    def __init__(self):
        self._h2h_cache: dict[tuple[int, int], tuple[int, float, float]] = {}
        self._competition_cache: dict[tuple[int, object, int], tuple[int, float, float]] = {}

    @staticmethod
    def _official(rows: list[Fixture]) -> list[Fixture]:
        return [row for row in rows if not classify_competition(row).excluded]

    def _h2h(self, fixture: Fixture, limit: int = 8) -> tuple[int, float, float]:
        cache_key = (int(fixture.id), int(limit))
        cached = self._h2h_cache.get(cache_key)
        if cached is not None:
            return cached

        rows = list(
            Fixture.objects.filter(kickoff__lt=fixture.kickoff)
            .filter(
                Q(home_team=fixture.home_team, away_team=fixture.away_team)
                | Q(home_team=fixture.away_team, away_team=fixture.home_team)
            )
            .filter(home_goals__isnull=False, away_goals__isnull=False)
            .select_related("competition_ref")
            .order_by("-kickoff")[: max(limit * 3, 20)]
        )
        rows = self._official(rows)[:limit]
        if not rows:
            result = (0, 0.50, 0.50)
        else:
            over = btts = 0
            for row in rows:
                hg, ag = int(row.home_goals or 0), int(row.away_goals or 0)
                over += int(hg + ag >= 3)
                btts += int(hg > 0 and ag > 0)
            n = len(rows)
            result = (n, over / n, btts / n)
        self._h2h_cache[cache_key] = result
        return result

    def _competition(self, fixture: Fixture, limit: int = 80) -> tuple[int, float, float]:
        if fixture.competition_ref_id is None:
            return 0, 0.50, 0.50

        # kickoff is deliberately part of the key: the original logic is
        # time-relative and must remain bit-for-bit equivalent for each fixture.
        cache_key = (int(fixture.competition_ref_id), fixture.kickoff, int(limit))
        cached = self._competition_cache.get(cache_key)
        if cached is not None:
            return cached

        rows = list(
            Fixture.objects.filter(
                competition_ref_id=fixture.competition_ref_id,
                kickoff__lt=fixture.kickoff,
                home_goals__isnull=False,
                away_goals__isnull=False,
            )
            .select_related("competition_ref")
            .order_by("-kickoff")[:limit]
        )
        rows = self._official(rows)
        if not rows:
            result = (0, 0.50, 0.50)
        else:
            over = btts = 0
            for row in rows:
                hg, ag = int(row.home_goals or 0), int(row.away_goals or 0)
                over += int(hg + ag >= 3)
                btts += int(hg > 0 and ag > 0)
            n = len(rows)
            result = (n, over / n, btts / n)
        self._competition_cache[cache_key] = result
        return result

    @staticmethod
    def _rate_score(rate: float, target: float = 0.65) -> float:
        return max(0.0, min(100.0, rate / target * 100.0))

    def evaluate(self, fixture: Fixture, features: FeatureVector, market: str) -> MarketConfidenceResult:
        h2h_n, h2h_over, h2h_btts = self._h2h(fixture)
        comp_n, comp_over, comp_btts = self._competition(fixture)
        failures: list[str] = []

        if market == "OVER_2_5":
            home_rate = float(features.home_over25_last5_home)
            away_rate = float(features.away_over25_last5_away)
            away_total_goals = float(features.away_profile.goals_for + features.away_profile.goals_against)

            score = (
                self._rate_score(home_rate) * 0.30
                + self._rate_score(away_rate) * 0.35
                + self._rate_score(h2h_over) * 0.20
                + self._rate_score(comp_over) * 0.15
            )

            if features.away_profile.sample_size >= 3 and away_rate < 0.35:
                score -= 15
                failures.append("away_over25_very_low")
            elif features.away_profile.sample_size >= 3 and away_rate < 0.45:
                score -= 8
                failures.append("away_over25_low")

            if features.away_profile.sample_size >= 3 and away_total_goals < 2.0:
                score -= 15
                failures.append("away_total_goals_very_low")
            elif features.away_profile.sample_size >= 3 and away_total_goals < 2.2:
                score -= 8
                failures.append("away_total_goals_low")

            if h2h_n >= 5 and h2h_over < 0.40:
                score -= 8
                failures.append("h2h_over25_low")
            if comp_n >= 20 and comp_over < 0.45:
                score -= 6
                failures.append("competition_over25_low")

            evidence = {
                "home_over25_rate": round(home_rate, 3),
                "away_over25_rate": round(away_rate, 3),
                "away_avg_total_goals": round(away_total_goals, 3),
                "h2h_sample": h2h_n,
                "h2h_over25_rate": round(h2h_over, 3),
                "competition_sample": comp_n,
                "competition_over25_rate": round(comp_over, 3),
            }
        elif market == "BTTS":
            home_rate = float(features.home_btts_last5_home)
            away_rate = float(features.away_btts_last5_away)
            score = (
                self._rate_score(home_rate) * 0.30
                + self._rate_score(away_rate) * 0.35
                + self._rate_score(h2h_btts) * 0.20
                + self._rate_score(comp_btts) * 0.15
            )
            if features.away_profile.sample_size >= 3 and features.away_failed_to_score_rate > 0.45:
                score -= 12
                failures.append("away_failed_to_score_high")
            if features.home_profile.sample_size >= 3 and features.home_clean_sheet_rate > 0.45:
                score -= 10
                failures.append("home_clean_sheet_high")
            if h2h_n >= 5 and h2h_btts < 0.40:
                score -= 8
                failures.append("h2h_btts_low")
            if comp_n >= 20 and comp_btts < 0.45:
                score -= 6
                failures.append("competition_btts_low")
            evidence = {
                "home_btts_rate": round(home_rate, 3),
                "away_btts_rate": round(away_rate, 3),
                "away_failed_to_score_rate": round(float(features.away_failed_to_score_rate), 3),
                "home_clean_sheet_rate": round(float(features.home_clean_sheet_rate), 3),
                "h2h_sample": h2h_n,
                "h2h_btts_rate": round(h2h_btts, 3),
                "competition_sample": comp_n,
                "competition_btts_rate": round(comp_btts, 3),
            }
        else:
            return MarketConfidenceResult(100.0, True, [], {})

        score = round(max(0.0, min(100.0, score)), 1)
        passed = score >= self.min_confidence
        if not passed:
            failures.append("market_confidence_below_50")
        return MarketConfidenceResult(score, passed, failures, evidence)
