from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .competition_quality import classify_competition
from .deep_analysis import DEEP_ANALYSIS_VERSION
from .models import DailyPremiumSelection, Prediction
from .score_v8 import V8_MODEL_VERSION


@dataclass(frozen=True)
class TierRule:
    name: str
    min_score: float
    min_edge: float
    min_ev: float
    min_btts_probability: float
    min_over25_probability: float


TIER_RULES = (
    TierRule("A", 92.0, 0.09, 0.10, 0.63, 0.65),
    TierRule("B", 88.0, 0.07, 0.08, 0.61, 0.63),
    TierRule("C", 84.0, 0.05, 0.06, 0.59, 0.61),
)

DYNAMIC_SCORE_FLOORS = (84.0, 82.0, 80.0)


class DailyPremiumSelector:
    """Select at most one Sprint 7.0 validated market per fixture and three picks per day."""

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
        # Sprint 7.0: only the deeply validated winner of BTTS vs Over for a
        # fixture can reach the daily ranking.
        if reasons.get("deep_analysis_version") != DEEP_ANALYSIS_VERSION:
            return False
        if reasons.get("deep_analysis_passed") is not True:
            return False
        if reasons.get("deep_preferred_market") is not True:
            return False
        if prediction.market_odds is None or prediction.edge is None or prediction.expected_value is None:
            return False
        probability = float(prediction.probability)
        if prediction.market == "BTTS":
            if probability < 0.59:
                return False
        elif prediction.market == "OVER_2_5":
            if probability < 0.61:
                return False
        else:
            return False
        return float(prediction.edge) >= 0.05 and float(prediction.expected_value) >= 0.06

    @classmethod
    def _tier_for(cls, prediction: Prediction, *, score_floor: float = 84.0) -> str | None:
        if not cls._passes_hard_value_floors(prediction):
            return None
        probability = float(prediction.probability)
        score = float(prediction.score)
        edge = float(prediction.edge)
        ev = float(prediction.expected_value)
        for rule in TIER_RULES:
            effective_score = rule.min_score
            if rule.name == "C":
                effective_score = min(effective_score, score_floor)
            if (
                score >= effective_score
                and edge >= rule.min_edge
                and ev >= rule.min_ev
                and probability >= cls._probability_floor(rule, prediction.market)
            ):
                return rule.name
        return None

    @classmethod
    def _rank_score(cls, prediction: Prediction) -> tuple[float, dict]:
        score_component = max(0.0, min(float(prediction.score), 100.0))
        probability_component = max(0.0, min(float(prediction.probability), 1.0)) * 100.0
        ev = max(0.0, float(prediction.expected_value or 0))
        edge = max(0.0, float(prediction.edge or 0))
        ev_component = min(ev / 0.25, 1.0) * 100.0
        edge_component = min(edge / 0.15, 1.0) * 100.0
        composite = (
            0.30 * score_component
            + 0.30 * ev_component
            + 0.25 * edge_component
            + 0.15 * probability_component
        )
        reasons = prediction.reasons or {}
        rationale = {
            "score_component": round(score_component, 2),
            "probability_component": round(probability_component, 2),
            "ev_component": round(ev_component, 2),
            "edge_component": round(edge_component, 2),
            "deep_analysis_version": reasons.get("deep_analysis_version"),
            "deep_analysis_evidence": reasons.get("deep_analysis_evidence") or {},
            "deep_analysis_warnings": reasons.get("deep_analysis_warnings") or [],
            "deep_score": reasons.get("deep_score"),
            "probability": float(prediction.probability),
            "market_odds": float(prediction.market_odds) if prediction.market_odds is not None else None,
            "edge": float(prediction.edge) if prediction.edge is not None else None,
            "expected_value": float(prediction.expected_value) if prediction.expected_value is not None else None,
            "formula": "0.30*deep_score + 0.30*ev + 0.25*edge + 0.15*probability",
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

        tier_priority = {"A": 3, "B": 2, "C": 1}
        ranked.sort(
            key=lambda item: (
                tier_priority[item[1]],
                item[2],
                float(item[0].expected_value or 0),
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
                market_odds__isnull=False,
                edge__isnull=False,
                expected_value__isnull=False,
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
