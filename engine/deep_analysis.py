from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from statistics import mean
from typing import Any

from django.db.models import Q

from .competition_quality import classify_competition
from .models import Fixture, Prediction, Team
from .score_v8 import V8_MODEL_VERSION


DEEP_ANALYSIS_VERSION = "sprint7.2"


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
    recent_sample_size: int
    recent_over25_rate: float
    recent_btts_rate: float
    recent_failed_to_score_rate: float


class DeepMatchAnalysisService:
    """Second-pass market validation using venue history plus recent-form stability."""

    def __init__(self, sample_size: int = 10):
        self.sample_size = max(5, min(int(sample_size), 10))

    @staticmethod
    def _decimal(value: float) -> Decimal:
        return Decimal(str(max(0.0, min(value, 100.0)))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @staticmethod
    def _base_score(prediction: Prediction) -> float:
        reasons = prediction.reasons or {}
        stored = reasons.get("score_before_deep_analysis")
        if stored is not None:
            try:
                return float(stored)
            except (TypeError, ValueError):
                pass
        return float(prediction.score or 0.0)

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
            return DeepVenueProfile(0, 0.5, 0.5, 1.2, 1.2, 2.4, 0.2, 0.2, 0.5, 0.5, 0, 0.5, 0.5, 0.2)

        gf_values: list[int] = []
        ga_values: list[int] = []
        overs = btts = clean = fts = btts_over = low_score = 0
        recent_overs = recent_btts = recent_fts = 0
        recent_n = min(5, len(fixtures))

        for index, item in enumerate(fixtures):
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
            if index < recent_n:
                recent_overs += int(total >= 3)
                recent_btts += int(both)
                recent_fts += int(gf == 0)

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
            recent_sample_size=recent_n,
            recent_over25_rate=round(recent_overs / recent_n, 3) if recent_n else 0.5,
            recent_btts_rate=round(recent_btts / recent_n, 3) if recent_n else 0.5,
            recent_failed_to_score_rate=round(recent_fts / recent_n, 3) if recent_n else 0.2,
        )

    @staticmethod
    def _value_component(prediction: Prediction) -> float:
        edge = max(0.0, float(prediction.edge or 0.0))
        ev = max(0.0, float(prediction.expected_value or 0.0))
        return min(100.0, 55.0 * min(edge / 0.15, 1.0) + 45.0 * min(ev / 0.25, 1.0))

    @staticmethod
    def _shortfall_penalty(rate: float, target: float, weight: float) -> float:
        return max(0.0, (target - rate) * weight)

    def _evaluate_market(self, prediction: Prediction, home: DeepVenueProfile, away: DeepVenueProfile) -> dict[str, Any]:
        base_score = self._base_score(prediction)
        value_component = self._value_component(prediction)
        sample_coverage = min(home.sample_size / self.sample_size, 1.0) * 0.5 + min(away.sample_size / self.sample_size, 1.0) * 0.5
        failures: list[str] = []
        warnings: list[str] = []
        market_support_penalty = 0.0
        side_consistency_penalty = 0.0
        coherence_penalty = 0.0
        recency_penalty = 0.0

        if prediction.market == "OVER_2_5":
            home_rate = home.over25_rate
            away_rate = away.over25_rate
            home_recent = home.recent_over25_rate if home.recent_sample_size >= 5 else home_rate
            away_recent = away.recent_over25_rate if away.recent_sample_size >= 5 else away_rate
            combined_rate = (home_rate + away_rate) / 2.0
            recent_combined_rate = (home_recent + away_recent) / 2.0
            avg_total = (home.avg_total_goals + away.avg_total_goals) / 2.0
            pattern_component = (0.70 * combined_rate + 0.30 * recent_combined_rate) * 100.0
            context_component = max(0.0, min(100.0, 50.0 + (avg_total - 2.5) * 20.0))
            market_support = (
                0.25 * home.over25_rate
                + 0.25 * away.over25_rate
                + 0.20 * home_recent
                + 0.20 * away_recent
                + 0.05 * home.btts_rate
                + 0.05 * away.btts_rate
            )

            if home.sample_size >= 5 and home_rate < 0.50:
                warnings.append("home_over25_deep_low")
            if away.sample_size >= 5 and away_rate < 0.50:
                warnings.append("away_over25_deep_low")
            if combined_rate < 0.50:
                warnings.append("combined_over25_deep_low")
            if market_support < 0.58:
                warnings.append("over25_market_support_low")
            if avg_total < 2.30:
                warnings.append("deep_low_total_goals")

            if home.sample_size >= 5:
                side_consistency_penalty += self._shortfall_penalty(home_rate, 0.50, 30.0)
            if away.sample_size >= 5:
                side_consistency_penalty += self._shortfall_penalty(away_rate, 0.50, 24.0)
            market_support_penalty = self._shortfall_penalty(market_support, 0.62, 24.0)

            if home.recent_sample_size >= 5:
                recency_penalty += self._shortfall_penalty(home_recent, 0.40, 20.0)
                if home_recent <= 0.20:
                    warnings.append("home_over25_recent_drought")
            if away.recent_sample_size >= 5:
                recency_penalty += self._shortfall_penalty(away_recent, 0.40, 20.0)
                if away_recent <= 0.20:
                    warnings.append("away_over25_recent_drought")

            if home.sample_size >= 6 and away.sample_size >= 6:
                weak_side = min(home_rate, away_rate)
                strong_side = max(home_rate, away_rate)
                if weak_side < 0.50 and strong_side >= 0.80 and market_support < 0.65:
                    coherence_penalty = 3.0
                    warnings.append("over25_one_sided_support")
                if combined_rate < 0.38:
                    failures.append("deep_over25_pattern_rejected")
                if avg_total < 2.05:
                    failures.append("deep_over25_low_score_rejected")
                if weak_side < 0.30 and market_support < 0.55:
                    failures.append("deep_over25_venue_contradiction")
                if recent_combined_rate <= 0.20 and combined_rate < 0.60:
                    failures.append("deep_over25_recent_form_rejected")
        else:
            home_rate = home.btts_rate
            away_rate = away.btts_rate
            home_recent = home.recent_btts_rate if home.recent_sample_size >= 5 else home_rate
            away_recent = away.recent_btts_rate if away.recent_sample_size >= 5 else away_rate
            combined_rate = (home_rate + away_rate) / 2.0
            recent_combined_rate = (home_recent + away_recent) / 2.0
            fts = (home.failed_to_score_rate + away.failed_to_score_rate) / 2.0
            recent_fts = (home.recent_failed_to_score_rate + away.recent_failed_to_score_rate) / 2.0
            pattern_component = (0.70 * combined_rate + 0.30 * recent_combined_rate) * 100.0
            context_fts = 0.70 * fts + 0.30 * recent_fts
            context_component = max(0.0, min(100.0, 100.0 - context_fts * 100.0))
            market_support = (
                0.25 * home.btts_rate
                + 0.25 * away.btts_rate
                + 0.20 * home_recent
                + 0.20 * away_recent
                + 0.05 * (1.0 - home.failed_to_score_rate)
                + 0.05 * (1.0 - away.failed_to_score_rate)
            )

            if home.sample_size >= 5 and home_rate < 0.50:
                warnings.append("home_btts_deep_low")
            if away.sample_size >= 5 and away_rate < 0.50:
                warnings.append("away_btts_deep_low")
            if combined_rate < 0.50:
                warnings.append("combined_btts_deep_low")
            if market_support < 0.58:
                warnings.append("btts_market_support_low")
            if fts > 0.35:
                warnings.append("deep_failed_to_score_high")

            if home.sample_size >= 5:
                side_consistency_penalty += self._shortfall_penalty(home_rate, 0.50, 28.0)
            if away.sample_size >= 5:
                side_consistency_penalty += self._shortfall_penalty(away_rate, 0.50, 28.0)
            market_support_penalty = self._shortfall_penalty(market_support, 0.62, 22.0)

            if home.recent_sample_size >= 5:
                recency_penalty += self._shortfall_penalty(home_recent, 0.40, 18.0)
                if home_recent <= 0.20:
                    warnings.append("home_btts_recent_drought")
            if away.recent_sample_size >= 5:
                recency_penalty += self._shortfall_penalty(away_recent, 0.40, 18.0)
                if away_recent <= 0.20:
                    warnings.append("away_btts_recent_drought")

            if home.sample_size >= 6 and away.sample_size >= 6:
                weak_side = min(home_rate, away_rate)
                strong_side = max(home_rate, away_rate)
                if weak_side < 0.50 and strong_side >= 0.80 and market_support < 0.65:
                    coherence_penalty = 3.0
                    warnings.append("btts_one_sided_support")
                if combined_rate < 0.38:
                    failures.append("deep_btts_pattern_rejected")
                if fts > 0.45:
                    failures.append("deep_btts_scoring_rejected")
                if weak_side < 0.30 and market_support < 0.55:
                    failures.append("deep_btts_venue_contradiction")
                if recent_combined_rate <= 0.20 and combined_rate < 0.60:
                    failures.append("deep_btts_recent_form_rejected")

        deep_score = 0.45 * base_score + 0.30 * pattern_component + 0.15 * value_component + 0.10 * context_component
        coverage_penalty = max(0.0, (0.70 - sample_coverage) * 12.0)
        warning_penalty = min(4.0, len(warnings) * 1.0)
        total_penalty = coverage_penalty + warning_penalty + market_support_penalty + side_consistency_penalty + coherence_penalty + recency_penalty
        deep_score = max(0.0, min(100.0, deep_score - total_penalty))

        evidence = {
            "home_sample": home.sample_size,
            "away_sample": away.sample_size,
            "home_over25_rate": home.over25_rate,
            "away_over25_rate": away.over25_rate,
            "home_btts_rate": home.btts_rate,
            "away_btts_rate": away.btts_rate,
            "home_recent_n": home.recent_sample_size,
            "away_recent_n": away.recent_sample_size,
            "home_recent_over25_rate": home.recent_over25_rate,
            "away_recent_over25_rate": away.recent_over25_rate,
            "home_recent_btts_rate": home.recent_btts_rate,
            "away_recent_btts_rate": away.recent_btts_rate,
            "home_avg_total_goals": home.avg_total_goals,
            "away_avg_total_goals": away.avg_total_goals,
            "home_avg_goals_for": home.avg_goals_for,
            "away_avg_goals_for": away.avg_goals_for,
            "home_failed_to_score_rate": home.failed_to_score_rate,
            "away_failed_to_score_rate": away.failed_to_score_rate,
            "home_recent_failed_to_score_rate": home.recent_failed_to_score_rate,
            "away_recent_failed_to_score_rate": away.recent_failed_to_score_rate,
            "home_clean_sheet_rate": home.clean_sheet_rate,
            "away_clean_sheet_rate": away.clean_sheet_rate,
            "home_gei": home.btts_over25_escalation_rate,
            "away_gei": away.btts_over25_escalation_rate,
            "sample_coverage": round(sample_coverage, 3),
            "market_support_index": round(market_support, 3),
            "pattern_component": round(pattern_component, 2),
            "context_component": round(context_component, 2),
            "value_component": round(value_component, 2),
            "coverage_penalty": round(coverage_penalty, 2),
            "warning_penalty": round(warning_penalty, 2),
            "market_support_penalty": round(market_support_penalty, 2),
            "side_consistency_penalty": round(side_consistency_penalty, 2),
            "coherence_penalty": round(coherence_penalty, 2),
            "recency_penalty": round(recency_penalty, 2),
            "total_deep_penalty": round(total_penalty, 2),
        }
        return {
            "passed": not failures,
            "score": round(deep_score, 2),
            "failures": failures,
            "warnings": warnings,
            "evidence": evidence,
            "base_score": round(base_score, 2),
        }

    @staticmethod
    def _canonical_state(evaluation: dict[str, Any], *, preferred: bool) -> dict[str, Any]:
        evidence = evaluation["evidence"]
        return {
            "version": DEEP_ANALYSIS_VERSION,
            "status": "complete",
            "passed": bool(evaluation["passed"]),
            "preferred_market": bool(preferred),
            "score": float(evaluation["score"]),
            "v8_score": float(evaluation["base_score"]),
            "warnings": list(evaluation["warnings"]),
            "failures": list(evaluation["failures"]),
            "home_n": int(evidence.get("home_sample") or 0),
            "away_n": int(evidence.get("away_sample") or 0),
            "home_over25": evidence.get("home_over25_rate"),
            "away_over25": evidence.get("away_over25_rate"),
            "home_btts": evidence.get("home_btts_rate"),
            "away_btts": evidence.get("away_btts_rate"),
            "market_support_index": evidence.get("market_support_index"),
            "total_penalty": evidence.get("total_deep_penalty"),
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
            evaluations.append((prediction, self._evaluate_market(prediction, home, away)))

        eligible = [(p, e) for p, e in evaluations if e["passed"] and p.market_odds is not None]
        preferred_id = None
        if eligible:
            preferred_id = max(
                eligible,
                key=lambda item: (item[1]["score"], float(item[0].expected_value or 0.0), float(item[0].edge or 0.0)),
            )[0].id

        updated: list[Prediction] = []
        for prediction, evaluation in evaluations:
            reasons = dict(prediction.reasons or {})
            original_v8_score = self._base_score(prediction)
            preferred = prediction.id == preferred_id
            deep_state = self._canonical_state(evaluation, preferred=preferred)
            reasons.update({
                "deep_analysis": deep_state,
                "deep_analysis_version": DEEP_ANALYSIS_VERSION,
                "deep_analysis_status": "complete",
                "deep_analysis_passed": bool(evaluation["passed"]),
                "deep_analysis_failures": evaluation["failures"],
                "deep_analysis_warnings": evaluation["warnings"],
                "deep_analysis_evidence": evaluation["evidence"],
                "score_before_deep_analysis": original_v8_score,
                "deep_score": evaluation["score"],
                "deep_preferred_market": preferred,
                "deep_home_n": deep_state["home_n"],
                "deep_away_n": deep_state["away_n"],
                "deep_home_over25": deep_state["home_over25"],
                "deep_away_over25": deep_state["away_over25"],
                "deep_home_btts": deep_state["home_btts"],
                "deep_away_btts": deep_state["away_btts"],
                "deep_market_support_index": deep_state["market_support_index"],
                "deep_total_penalty": deep_state["total_penalty"],
                "deep_recency_penalty": evaluation["evidence"].get("recency_penalty"),
                "deep_warnings": deep_state["warnings"],
            })
            prediction.score = self._decimal(evaluation["score"])
            prediction.reasons = reasons
            prediction.save(update_fields=["score", "reasons"])
            updated.append(prediction)
        return updated