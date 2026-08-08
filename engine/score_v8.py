from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.conf import settings

from .features import FeatureEngineeringService, FeatureVector
from .market_confidence import MarketConfidenceService
from .model import FootballValueEngine
from .models import Fixture, Prediction
from .quantitative import MarketQuote, MatchContext, TeamProfile


V8_MODEL_VERSION = "v8.0-sprint4-score"


@dataclass(frozen=True)
class V8RuleConfig:
    min_data_quality: float = 60.0
    min_venue_sample: int = 3
    lineup_rotation_threshold: float = 0.55
    lineup_rotation_attack_factor: float = 0.90
    lineup_mild_rotation_threshold: float = 0.73
    lineup_mild_attack_factor: float = 0.96
    market_confidence_neutral: float = 65.0
    market_confidence_penalty_factor: float = 0.45


class ScoreEngineV8:
    """V8 scoring layer built on reproducible persisted features.

    V7's Poisson/value engine remains the quantitative core. V8 adds explicit
    data-quality/sample-size gates, lineup adjustments and Sprint 6.4 market
    context validation. Every decision is written to `reasons` so backtesting
    can isolate the value of each rule instead of treating the model as a black
    box.
    """

    def __init__(self, config: V8RuleConfig | None = None):
        self.config = config or V8RuleConfig()
        self.core = FootballValueEngine()
        self.market_confidence = MarketConfidenceService()
        self.core.min_edge = float(getattr(settings, "MIN_EDGE", self.core.min_edge))
        self.core.min_ev = float(getattr(settings, "MIN_EV", self.core.min_ev))

    @staticmethod
    def _round_number(fixture: Fixture) -> int | None:
        text = fixture.round or ""
        digits = "".join(ch for ch in text if ch.isdigit())
        return int(digits) if digits else None

    def _lineup_factor(self, continuity: float | None) -> tuple[float, str]:
        if continuity is None:
            return 1.0, "lineup_unknown"
        if continuity < self.config.lineup_rotation_threshold:
            return self.config.lineup_rotation_attack_factor, "heavy_rotation"
        if continuity < self.config.lineup_mild_rotation_threshold:
            return self.config.lineup_mild_attack_factor, "mild_rotation"
        return 1.0, "stable_lineup"

    def _context_from_features(self, fixture: Fixture, features: FeatureVector) -> tuple[MatchContext, dict[str, Any]]:
        home = features.home_profile
        away = features.away_profile
        home_factor, home_lineup_state = self._lineup_factor(features.home_lineup_continuity)
        away_factor, away_lineup_state = self._lineup_factor(features.away_lineup_continuity)

        home_profile = TeamProfile(
            goals_for=home.goals_for,
            goals_against=home.goals_against,
            xg_for=home.goals_for,
            xg_against=home.goals_against,
            over25_rate=home.over25_rate,
            btts_rate=home.btts_rate,
            clean_sheet_rate=home.clean_sheet_rate,
            failed_to_score_rate=home.failed_to_score_rate,
            sample_size=home.sample_size,
        )
        away_profile = TeamProfile(
            goals_for=away.goals_for,
            goals_against=away.goals_against,
            xg_for=away.goals_for,
            xg_against=away.goals_against,
            over25_rate=away.over25_rate,
            btts_rate=away.btts_rate,
            clean_sheet_rate=away.clean_sheet_rate,
            failed_to_score_rate=away.failed_to_score_rate,
            sample_size=away.sample_size,
        )

        context = MatchContext(
            home=home_profile,
            away=away_profile,
            round_number=self._round_number(fixture),
            home_over25_last5_home=features.home_over25_last5_home,
            away_over25_last5_away=features.away_over25_last5_away,
            home_btts_last5_home=features.home_btts_last5_home,
            away_btts_last5_away=features.away_btts_last5_away,
            lineup_attack_factor_home=home_factor,
            lineup_attack_factor_away=away_factor,
        )
        audit = {
            "feature_model_version": V8_MODEL_VERSION,
            "data_quality_score": features.data_quality_score,
            "home_venue_sample": home.sample_size,
            "away_venue_sample": away.sample_size,
            "home_lineup_continuity": features.home_lineup_continuity,
            "away_lineup_continuity": features.away_lineup_continuity,
            "home_lineup_state": home_lineup_state,
            "away_lineup_state": away_lineup_state,
            "xg_source": "venue_goals_proxy",
        }
        return context, audit

    def _gates(self, features: FeatureVector) -> tuple[bool, list[str]]:
        failures: list[str] = []
        if features.data_quality_score < self.config.min_data_quality:
            failures.append("insufficient_data_quality")
        if features.home_profile.sample_size < self.config.min_venue_sample:
            failures.append("insufficient_home_venue_sample")
        if features.away_profile.sample_size < self.config.min_venue_sample:
            failures.append("insufficient_away_venue_sample")
        return not failures, failures

    def _adjust_score_for_market_confidence(self, raw_score: float, confidence: float) -> tuple[float, float]:
        shortfall = max(0.0, self.config.market_confidence_neutral - confidence)
        penalty = round(shortfall * self.config.market_confidence_penalty_factor, 1)
        return round(max(0.0, float(raw_score) - penalty), 1), penalty

    def evaluate(self, fixture: Fixture, features: FeatureVector | None = None) -> dict[str, Any]:
        features = features or FeatureEngineeringService().build(fixture)
        context, audit = self._context_from_features(fixture, features)

        btts_quote = (
            MarketQuote(features.btts_market_odds, getattr(settings, "PREFERRED_BOOKMAKER", "Betano"))
            if features.btts_market_odds
            else None
        )
        over_quote = (
            MarketQuote(features.over25_market_odds, getattr(settings, "PREFERRED_BOOKMAKER", "Betano"))
            if features.over25_market_odds
            else None
        )

        core_result = self.core.evaluate(context, btts_quote=btts_quote, over25_quote=over_quote)
        base_gates_passed, base_gate_failures = self._gates(features)

        result: dict[str, Any] = {}
        for key, evaluation in core_result.items():
            market_check = self.market_confidence.evaluate(fixture, features, evaluation.market)
            adjusted_score, confidence_penalty = self._adjust_score_for_market_confidence(
                evaluation.score,
                market_check.score,
            )
            gate_failures = list(base_gate_failures)
            if not market_check.passed:
                gate_failures.extend(market_check.failures)
            gates_passed = base_gates_passed and market_check.passed

            reasons = dict(evaluation.reasons)
            reasons.update(audit)
            reasons["v8_gates_passed"] = gates_passed
            reasons["v8_gate_failures"] = gate_failures
            reasons["core_tier_before_v8_gates"] = evaluation.tier
            reasons["core_score_before_market_confidence"] = evaluation.score
            reasons["market_confidence_score"] = market_check.score
            reasons["market_confidence_passed"] = market_check.passed
            reasons["market_confidence_failures"] = market_check.failures
            reasons["market_confidence_evidence"] = market_check.evidence
            reasons["market_confidence_penalty"] = confidence_penalty
            reasons["model_version"] = V8_MODEL_VERSION

            tier = evaluation.tier if gates_passed else ""
            result[key] = {
                "market": evaluation.market,
                "selection": evaluation.selection,
                "probability": evaluation.probability,
                "fair_odds": evaluation.fair_odds,
                "market_odds": evaluation.market_odds,
                "implied_probability": evaluation.implied_probability,
                "edge": evaluation.edge,
                "expected_value": evaluation.expected_value,
                "score": adjusted_score,
                "tier": tier,
                "reasons": reasons,
            }
        return result

    def evaluate_and_persist(self, fixture: Fixture, features: FeatureVector | None = None) -> dict[str, Any]:
        result = self.evaluate(fixture, features)
        for evaluation in result.values():
            Prediction.objects.update_or_create(
                fixture=fixture,
                model_version=V8_MODEL_VERSION,
                market=evaluation["market"],
                selection=evaluation["selection"],
                defaults={
                    "probability": evaluation["probability"],
                    "fair_odds": evaluation["fair_odds"],
                    "market_odds": evaluation["market_odds"],
                    "edge": evaluation["edge"],
                    "expected_value": evaluation["expected_value"],
                    "score": evaluation["score"],
                    "tier": evaluation["tier"],
                    "reasons": evaluation["reasons"],
                },
            )
        return result
