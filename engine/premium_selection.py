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


# Sprint 7.5.1: the absolute probability floor is a MODEL evidence floor and
# therefore uses raw model probability. Market calibration is used for what it
# is designed for: calibrated edge, EV and reliability. Using the already
# market-shrunk probability again as an absolute floor was a double penalty.
TIER_RULES = (
    TierRule("A", 90.0, 0.07, float(TIER_A_MIN_EV), 0.58, 0.60, TIER_A_MIN_RELIABILITY),
    TierRule("B", 84.0, 0.05, float(PREMIUM_MIN_EV), 0.54, 0.56, PREMIUM_MIN_RELIABILITY),
)

# Tier B may relax only the composite score. All hard professional gates stay
# fixed: official competition, odds 1.60-2.40, Deep pass/preferred market,
# reliability, calibrated edge and reliable EV. This prevents redundant score
# gating from rejecting an otherwise fully validated value position.
DYNAMIC_SCORE_FLOORS = (84.0, 82.0, 80.0, 78.0, 76.0)


class DailyPremiumSelector:
    """Select up to three Deep-validated, market-calibrated Premium Value markets."""

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

    @staticmethod
    def _base_probability_floor(market: str) -> float:
        if market == "BTTS":
            return 0.54
        if market == "OVER_2_5":
            return 0.56
        return 1.0

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
        if prediction.market not in {"BTTS", "OVER_2_5"}:
            return False

        # IMPORTANT: use raw model probability for statistical plausibility.
        # The calibrated probability has already been shrunk toward market price;
        # applying the same absolute floor to it double-counts the bookmaker.
        if calibration.raw_probability < cls._base_probability_floor(prediction.market):
            return False
        if calibration.calibrated_edge < 0.05:
            return False
        if calibration.reliable_ev < float(PREMIUM_MIN_EV):
            return False
        return True

    @classmethod
    def rejection_reasons(cls, prediction: Prediction, *, score_floor: float = 76.0) -> list[str]:
        reasons_out: list[str] = []
        quality = classify_competition(prediction.fixture)
        if quality.excluded:
            reasons_out.append(f"competition:{quality.reason}")
            return reasons_out
        reasons = prediction.reasons or {}
        if not reasons.get("v8_gates_passed", False):
            reasons_out.append("v8_gates")
        if reasons.get("deep_analysis_version") != DEEP_ANALYSIS_VERSION:
            reasons_out.append("deep_missing")
        elif reasons.get("deep_analysis_passed") is not True:
            reasons_out.append("deep_rejected")
        if reasons.get("deep_preferred_market") is not True:
            reasons_out.append("not_deep_preferred")
        if prediction.market_odds is None:
            reasons_out.append("no_odds")
            return reasons_out
        if not is_premium_value_odds(prediction.market_odds):
            reasons_out.append("odds_outside_1.60_2.40")
        calibration = cls.calibrator.calibrate(prediction)
        if calibration.reliability < PREMIUM_MIN_RELIABILITY:
            reasons_out.append(f"reliability:{calibration.reliability:.3f}")
        if calibration.raw_probability < cls._base_probability_floor(prediction.market):
            reasons_out.append(f"raw_probability:{calibration.raw_probability:.3f}")
        if calibration.calibrated_edge < 0.05:
            reasons_out.append(f"calibrated_edge:{calibration.calibrated_edge:.3f}")
        if calibration.reliable_ev < float(PREMIUM_MIN_EV):
            reasons_out.append(f"reliable_ev:{calibration.reliable_ev:.3f}")
        if float(prediction.score or 0.0) < score_floor:
            reasons_out.append(f"score:{float(prediction.score or 0.0):.1f}")
        return reasons_out

    @classmethod
    def _tier_for(cls, prediction: Prediction, *, score_floor: float = 84.0) -> str | None:
        if not cls._passes_hard_value_floors(prediction):
            return None

        calibration = cls.calibrator.calibrate(prediction)
        # Raw probability is the model-side plausibility check. Calibrated edge,
        # EV and reliability are the market-side value checks.
        probability = calibration.raw_probability
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
        probability_component = calibration.raw_probability * 100.0
        ev_component = min(max(0.0, calibration.reliable_ev) / 0.20, 1.0) * 100.0
        edge_component = min(max(0.0, calibration.calibrated_edge) / 0.12, 1.0) * 100.0
        reliability_component = calibration.reliability_score

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
            "market_implied_probability": calibration.implied_probability,
            "market_odds": float(prediction.market_odds) if prediction.market_odds is not None else None,
            "raw_edge": calibration.raw_edge,
            "calibrated_edge": calibration.calibrated_edge,
            "raw_expected_value": calibration.raw_ev,
            "calibrated_expected_value": calibration.calibrated_ev,
            "reliable_expected_value": calibration.reliable_ev,
            "probability_reliability": calibration.reliability,
            "odds_policy": "Premium Value 1.60-2.40",
            "value_gate": "Sprint 7.5.1 raw probability + calibrated edge/EV + Deep + reliability",
            "formula": "0.22*deep_score + 0.30*reliable_ev + 0.20*calibrated_edge + 0.13*raw_probability + 0.15*reliability",
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

    @staticmethod
    def _unique_fixture_count(ranked) -> int:
        return len({item[0].fixture_id for item in ranked})

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
            current = self._rank_candidates(candidates, score_floor)
            ranked = current
            selected_floor = score_floor
            if self._unique_fixture_count(current) >= self.max_picks:
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
