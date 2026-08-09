from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .competition_quality import classify_competition
from .deep_analysis import DEEP_ANALYSIS_VERSION
from .models import DailyPremiumSelection, Prediction
from .probability_calibration import (
    PREMIUM_MIN_RELIABILITY,
    TIER_A_MIN_RELIABILITY,
    ProbabilityEVCalibrationService,
)
from .score_v8 import V8_MODEL_VERSION
from .value_policy import (
    PREMIUM_MIN_EV,
    TIER_A_MIN_EV,
    is_premium_value_odds,
)


@dataclass(frozen=True)
class TierRule:
    name: str
    min_score: float
    min_edge: float
    min_ev: float
    min_btts_probability: float
    min_over25_probability: float
    min_reliability: float


# Sprint 7.3: probability/EV used by Premium are calibrated, not raw.
# EV thresholds apply to reliable_ev, so weak evidence cannot manufacture value.
TIER_RULES = (
    TierRule("A", 90.0, 0.07, float(TIER_A_MIN_EV), 0.63, 0.65, TIER_A_MIN_RELIABILITY),
    TierRule("B", 84.0, 0.05, float(PREMIUM_MIN_EV), 0.59, 0.61, PREMIUM_MIN_RELIABILITY),
)

DYNAMIC_SCORE_FLOORS = (84.0, 82.0, 80.0)


class DailyPremiumSelector:
    """Select at most one Deep-validated, probability-calibrated Premium Value market per fixture."""

    calibrator = ProbabilityEVCalibrationService()

    def __init__(self, model_version: str = V8_MODEL_VERSION, max_picks: int = 3):
        self.model_version = model_version
        self.max_picks = max(1, min(int(max_picks), 3))

    @staticmethod
    def _bounds(target_date: date):
        start = timezone.make_aware(datetime.combine(target_date, time.min))
        return start, start + timedelta(days=1)

    @staticmethod
    def _probability_floor(rule: TierRule, market: str) -> float:
        return rule.min_btts_probability if market == "BTTS" else rule.min_over25_probability

    @classmethod
    def _passes_hard_value_floors(cls, prediction: Prediction) -> bool:
        if classify_competition(prediction.fixture).excluded:
            return False
        reasons = prediction.reasons or {}
        if not reasons.get("v8_gates_passed", False):
            return False
        if reasons.get("deep_analysis_version") != DEEP_ANALYSIS_VERSION:
            return False
        if reasons.get("deep_analysis_passed") is not True:
            return False
        if reasons.get("deep_preferred_market") is not True:
            return False
        if prediction.market_odds is None or prediction.edge is None or prediction.expected_value is None:
            return False
        if not is_premium_value_odds(prediction.market_odds):
            return False

        calibration = cls.calibrator.calibrate(prediction)
        if not calibration.premium_reliable:
            return False

        probability = calibration.calibrated_probability
        if prediction.market == "BTTS":
            if probability < 0.59:
                return False
        elif prediction.market == "OVER_2_5":
            if probability < 0.61:
                return False
        else:
            return False

        return (
            calibration.calibrated_edge >= 0.05
            and calibration.reliable_ev >= float(PREMIUM_MIN_EV)
        )

    @classmethod
    def _tier_for(cls, prediction: Prediction, *, score_floor: float = 84.0) -> str | None:
        if not cls._passes_hard_value_floors(prediction):
            return None

        calibration = cls.calibrator.calibrate(prediction)
        probability = calibration.calibrated_probability
        score = float(prediction.score)
        edge = calibration.calibrated_edge
        ev = calibration.reliable_ev

        for rule in TIER_RULES:
            effective_score = rule.min_score
            if rule.name == "B":
                effective_score = min(effective_score, score_floor)
            if (
                score >= effective_score
                and edge >= rule.min_edge
                and ev >= rule.min_ev
                and probability >= cls._probability_floor(rule, prediction.market)
                and calibration.reliability >= rule.min_reliability
            ):
                return rule.name
        return None

    @classmethod
    def _rank_score(cls, prediction: Prediction) -> tuple[float, dict]:
        calibration = cls.calibrator.calibrate(prediction)
        score_component = max(0.0, min(float(prediction.score), 100.0))
        probability_component = calibration.calibrated_probability * 100.0
        ev_component = min(max(0.0, calibration.reliable_ev) / 0.20, 1.0) * 100.0
        edge_component = min(max(0.0, calibration.calibrated_edge) / 0.12, 1.0) * 100.0
        reliability_component = calibration.reliability_score

        # Sprint 7.3: reliable EV dominates; raw probability/EV no longer rank directly.
        composite = (
            0.22 * score_component
            + 0.30 * ev_component
            + 0.20 * edge_component
            + 0.13 * probability_component
            + 0.15 * reliability_component
        )
        reasons = prediction.reasons or {}
        rationale = {
            "score_component": round(score_component, 2),
            "probability_component": round(probability_component, 2),
            "ev_component": round(ev_component, 2),
            "edge_component": round(edge_component, 2),
            "reliability_component": round(reliability_component, 2),
            "deep_analysis_version": reasons.get("deep_analysis_version"),
            "deep_analysis_evidence": reasons.get("deep_analysis_evidence") or {},
            "deep_analysis_warnings": reasons.get("deep_analysis_warnings") or [],
            "deep_score": reasons.get("deep_score"),
            "probability_calibration": calibration.as_dict(),
            "raw_probability": calibration.raw_probability,
            "calibrated_probability": calibration.calibrated_probability,
            "market_odds": float(prediction.market_odds) if prediction.market_odds is not None else None,
            "raw_edge": calibration.raw_edge,
            "calibrated_edge": calibration.calibrated_edge,
            "raw_expected_value": calibration.raw_ev,
            "calibrated_expected_value": calibration.calibrated_ev,
            "reliable_expected_value": calibration.reliable_ev,
            "probability_reliability": calibration.reliability,
            "odds_policy": "Premium Value 1.60-2.40",
            "formula": "0.22*deep_score + 0.30*reliable_ev + 0.20*calibrated_edge + 0.13*calibrated_probability + 0.15*reliability",
        }
        return round(composite, 2), rationale

    def _rank_candidates(self, candidates: list[Prediction], score_floor: float):
        ranked = []
        for prediction in candidates:
            if classify_competition(prediction.fixture).excluded:
                continue
            tier = self._tier_for(prediction, score_floor=score_floor)
            if tier is None:
                continue
            rank_score, rationale = self._rank_score(prediction)
            rationale["premium_tier"] = tier
            rationale["dynamic_score_floor"] = score_floor
            ranked.append((prediction, tier, rank_score, rationale))

        tier_priority = {"A": 2, "B": 1}
        ranked.sort(
            key=lambda item: (
                tier_priority[item[1]],
                item[2],
                float(item[3].get("reliable_expected_value") or 0),
                float(item[0].score),
            ),
            reverse=True,
        )
        return ranked

    @transaction.atomic
    def select(self, target_date: date) -> list[DailyPremiumSelection]:
        start, end = self._bounds(target_date)
        future_start = max(start, timezone.now())
        candidates = list(
            Prediction.objects.select_related(
                "fixture",
                "fixture__home_team",
                "fixture__away_team",
                "fixture__competition_ref",
            )
            .filter(
                model_version=self.model_version,
                fixture__kickoff__gte=future_start,
                fixture__kickoff__lt=end,
                market_odds__gte=Decimal("1.60"),
                market_odds__lte=Decimal("2.40"),
                edge__isnull=False,
                expected_value__gte=PREMIUM_MIN_EV,
            )
        )

        ranked = []
        selected_floor = DYNAMIC_SCORE_FLOORS[-1]
        for score_floor in DYNAMIC_SCORE_FLOORS:
            ranked = self._rank_candidates(candidates, score_floor)
            selected_floor = score_floor
            if ranked:
                break

        chosen = []
        seen_fixtures = set()
        for item in ranked:
            prediction = item[0]
            if prediction.fixture_id in seen_fixtures:
                continue
            chosen.append(item)
            seen_fixtures.add(prediction.fixture_id)
            if len(chosen) >= self.max_picks:
                break

        DailyPremiumSelection.objects.filter(
            target_date=target_date,
            model_version=self.model_version,
        ).delete()

        rows = []
        for index, (prediction, tier, rank_score, rationale) in enumerate(chosen, start=1):
            rationale["selector_dynamic_floor_used"] = selected_floor
            rows.append(
                DailyPremiumSelection(
                    target_date=target_date,
                    prediction=prediction,
                    rank=index,
                    premium_tier=tier,
                    premium_rank_score=Decimal(f"{rank_score:.2f}"),
                    model_version=self.model_version,
                    rationale=rationale,
                )
            )
        if rows:
            DailyPremiumSelection.objects.bulk_create(rows)
        return list(
            DailyPremiumSelection.objects.select_related(
                "prediction",
                "prediction__fixture",
                "prediction__fixture__home_team",
                "prediction__fixture__away_team",
                "prediction__fixture__competition_ref",
            )
            .filter(target_date=target_date, model_version=self.model_version)
            .order_by("rank")
        )
