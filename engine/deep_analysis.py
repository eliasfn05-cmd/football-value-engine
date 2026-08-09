from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from statistics import mean
from typing import Any

from django.db.models import Q

from .competition_quality import classify_competition
from .models import Fixture, Prediction, Team
from .score_v8 import V8_MODEL_VERSION


DEEP_ANALYSIS_VERSION = "sprint7.0"


@dataclass(frozen=True)
class DeepVenueProfile:
    sample_size: int
    over25_rate: float
    btts_rate: float
    avg_goals_for: float
    avg_goals_against: float
    avg_total_goals: float
    clean_sheet_rate: float
    failed_to_score_rate: float
    btts_over25_escalation_rate: float
    low_score_rate: float


class DeepMatchAnalysisService:
    """Second-pass market validation using up to 10 official venue matches.

    V8 remains the quantitative core. Sprint 7.0 validates only the strongest
    fixtures with deeper persisted history and chooses one preferred market per
    fixture before the operational Premium ranking.
    """

    def __init__(self, sample_size: int = 10):
        self.sample_size = max(5, min(int(sample_size), 10))

    @staticmethod
    def _decimal(value: float) -> Decimal:
        return Decimal(str(max(0.0, min(value, 100.0)))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def _venue_fixtures(self, team: Team, venue: str, before_fixture: Fixture) -> list[Fixture]:
        qs = (
            Fixture.objects.filter(kickoff__lt=before_fixture.kickoff)
            .filter(home_goals__isnull=False, away_goals__isnull=False)
            .select_related("home_team", "away_team", "competition_ref")
            .order_by("-kickoff")
        )
        qs = qs.filter(home_team=team) if venue == "home" else qs.filter(away_team=team)
        rows: list[Fixture] = []
        for item in qs.iterator(chunk_size=100):
            if classify_competition(item).excluded:
                continue
            rows.append(item)
            if len(rows) >= self.sample_size:
                break
        return rows

    def venue_profile(self, team: Team, venue: str, before_fixture: Fixture) -> DeepVenueProfile:
        fixtures = self._venue_fixtures(team, venue, before_fixture)
        if not fixtures:
            return DeepVenueProfile(0, 0.5, 0.5, 1.2, 1.2, 2.4, 0.2, 0.2, 0.5, 0.5)

        gf_values: list[int] = []
        ga_values: list[int] = []
        overs = btts = clean = fts = btts_over = low_score = 0
        for item in fixtures:
            hg = int(item.home_goals or 0)
            ag = int(item.away_goals or 0)
            gf, ga = (hg, ag) if venue == "home" else (ag, hg)
            total = gf + ga
            both = gf > 0 and ga > 0
            gf_values.append(gf)
            ga_values.append(ga)
            overs += int(total >= 3)
            btts += int(both)
            clean += int(ga == 0)
            fts += int(gf == 0)
            btts_over += int(both and total >= 3)
            low_score += int(total <= 2)

        n = len(fixtures)
        return DeepVenueProfile(
            sample_size=n,
            over25_rate=round(overs / n, 3),
            btts_rate=round(btts / n, 3),
            avg_goals_for=round(mean(gf_values), 3),
            avg_goals_against=round(mean(ga_values), 3),
            avg_total_goals=round(mean([a + b for a, b in zip(gf_values, ga_values)]), 3),
            clean_sheet_rate=round(clean / n, 3),
            failed_to_score_rate=round(fts / n, 3),
            btts_over25_escalation_rate=round((btts_over / btts) if btts else 0.5, 3),
            low_score_rate=round(low_score / n, 3),
        )

    @staticmethod
    def _value_component(prediction: Prediction) -> float:
        edge = max(0.0, float(prediction.edge or 0.0))
        ev = max(0.0, float(prediction.expected_value or 0.0))
        return min(100.0, 55.0 * min(edge / 0.15, 1.0) + 45.0 * min(ev / 0.25, 1.0))

    def _evaluate_market(
        self,
        prediction: Prediction,
        home: DeepVenueProfile,
        away: DeepVenueProfile,
    ) -> dict[str, Any]:
        base_score = float(prediction.score or 0.0)
        value_component = self._value_component(prediction)
        sample_coverage = min(home.sample_size / self.sample_size, 1.0) * 0.5 + min(away.sample_size / self.sample_size, 1.0) * 0.5
        failures: list[str] = []
        warnings: list[str] = []

        if prediction.market == "OVER_2_5":
            home_rate = home.over25_rate
            away_rate = away.over25_rate
            combined_rate = (home_rate + away_rate) / 2.0
            avg_total = (home.avg_total_goals + away.avg_total_goals) / 2.0
            pattern_component = combined_rate * 100.0
            context_component = max(0.0, min(100.0, 50.0 + (avg_total - 2.5) * 20.0))

            if home.sample_size >= 5 and home_rate < 0.40:
                warnings.append("home_over25_deep_low")
            if away.sample_size >= 5 and away_rate < 0.40:
                warnings.append("away_over25_deep_low")
            if combined_rate < 0.45:
                warnings.append("combined_over25_deep_low")
            if avg_total < 2.30:
                warnings.append("deep_low_total_goals")
            if home.sample_size >= 6 and away.sample_size >= 6 and combined_rate < 0.38:
                failures.append("deep_over25_pattern_rejected")
            if home.sample_size >= 6 and away.sample_size >= 6 and avg_total < 2.05:
                failures.append("deep_over25_low_score_rejected")
        else:
            home_rate = home.btts_rate
            away_rate = away.btts_rate
            combined_rate = (home_rate + away_rate) / 2.0
            fts = (home.failed_to_score_rate + away.failed_to_score_rate) / 2.0
            pattern_component = combined_rate * 100.0
            context_component = max(0.0, min(100.0, 100.0 - fts * 100.0))

            if home.sample_size >= 5 and home_rate < 0.40:
                warnings.append("home_btts_deep_low")
            if away.sample_size >= 5 and away_rate < 0.40:
                warnings.append("away_btts_deep_low")
            if combined_rate < 0.45:
                warnings.append("combined_btts_deep_low")
            if fts > 0.35:
                warnings.append("deep_failed_to_score_high")
            if home.sample_size >= 6 and away.sample_size >= 6 and combined_rate < 0.38:
                failures.append("deep_btts_pattern_rejected")
            if home.sample_size >= 6 and away.sample_size >= 6 and fts > 0.45:
                failures.append("deep_btts_scoring_rejected")

        deep_score = (
            0.45 * base_score
            + 0.30 * pattern_component
            + 0.15 * value_component
            + 0.10 * context_component
        )
        # Sparse deep history does not hard-reject, but it cannot earn full trust.
        coverage_penalty = max(0.0, (0.70 - sample_coverage) * 12.0)
        warning_penalty = min(12.0, len(warnings) * 3.0)
        deep_score = max(0.0, min(100.0, deep_score - coverage_penalty - warning_penalty))

        evidence = {
            "home_sample": home.sample_size,
            "away_sample": away.sample_size,
            "home_over25_rate": home.over25_rate,
            "away_over25_rate": away.over25_rate,
            "home_btts_rate": home.btts_rate,
            "away_btts_rate": away.btts_rate,
            "home_avg_total_goals": home.avg_total_goals,
            "away_avg_total_goals": away.avg_total_goals,
            "home_avg_goals_for": home.avg_goals_for,
            "away_avg_goals_for": away.avg_goals_for,
            "home_failed_to_score_rate": home.failed_to_score_rate,
            "away_failed_to_score_rate": away.failed_to_score_rate,
            "home_clean_sheet_rate": home.clean_sheet_rate,
            "away_clean_sheet_rate": away.clean_sheet_rate,
            "home_gei": home.btts_over25_escalation_rate,
            "away_gei": away.btts_over25_escalation_rate,
            "sample_coverage": round(sample_coverage, 3),
            "pattern_component": round(pattern_component, 2),
            "context_component": round(context_component, 2),
            "value_component": round(value_component, 2),
            "coverage_penalty": round(coverage_penalty, 2),
            "warning_penalty": round(warning_penalty, 2),
        }
        return {
            "passed": not failures,
            "score": round(deep_score, 2),
            "failures": failures,
            "warnings": warnings,
            "evidence": evidence,
        }

    def analyze_fixture(self, fixture: Fixture) -> list[Prediction]:
        home = self.venue_profile(fixture.home_team, "home", fixture)
        away = self.venue_profile(fixture.away_team, "away", fixture)
        predictions = list(
            Prediction.objects.filter(fixture=fixture, model_version=V8_MODEL_VERSION)
            .filter(Q(market="BTTS") | Q(market="OVER_2_5"))
        )
        if not predictions:
            return []

        evaluations: list[tuple[Prediction, dict[str, Any]]] = []
        for prediction in predictions:
            evaluation = self._evaluate_market(prediction, home, away)
            evaluations.append((prediction, evaluation))

        eligible = [(p, e) for p, e in evaluations if e["passed"] and p.market_odds is not None]
        preferred_id = None
        if eligible:
            preferred_id = max(
                eligible,
                key=lambda item: (
                    item[1]["score"],
                    float(item[0].expected_value or 0.0),
                    float(item[0].edge or 0.0),
                ),
            )[0].id

        updated: list[Prediction] = []
        for prediction, evaluation in evaluations:
            reasons = dict(prediction.reasons or {})
            reasons.update({
                "deep_analysis_version": DEEP_ANALYSIS_VERSION,
                "deep_analysis_passed": bool(evaluation["passed"]),
                "deep_analysis_failures": evaluation["failures"],
                "deep_analysis_warnings": evaluation["warnings"],
                "deep_analysis_evidence": evaluation["evidence"],
                "score_before_deep_analysis": float(prediction.score),
                "deep_score": evaluation["score"],
                "deep_preferred_market": prediction.id == preferred_id,
            })
            prediction.score = self._decimal(evaluation["score"])
            prediction.reasons = reasons
            prediction.save(update_fields=["score", "reasons"])
            updated.append(prediction)
        return updated
