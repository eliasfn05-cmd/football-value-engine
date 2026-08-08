from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

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


class DailyPremiumSelector:
    """Select at most one market per fixture and at most three picks per day.

    Raw V8 predictions are never rewritten. Operational selections live in
    DailyPremiumSelection so backtesting evidence remains untouched.
    """

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
    def _tier_for(cls, prediction: Prediction) -> str | None:
        reasons = prediction.reasons or {}
        if not reasons.get("v8_gates_passed", False):
            return None
        if prediction.market_odds is None or prediction.edge is None or prediction.expected_value is None:
            return None

        probability = float(prediction.probability)
        score = float(prediction.score)
        edge = float(prediction.edge)
        ev = float(prediction.expected_value)
        for rule in TIER_RULES:
            if (
                score >= rule.min_score
                and edge >= rule.min_edge
                and ev >= rule.min_ev
                and probability >= cls._probability_floor(rule, prediction.market)
            ):
                return rule.name
        return None

    @staticmethod
    def _quote_quality(prediction: Prediction) -> float:
        bookmaker = str((prediction.reasons or {}).get("bookmaker") or "").strip().lower()
        if bookmaker == "betano":
            return 100.0
        if bookmaker:
            return 75.0
        return 0.0

    @classmethod
    def _rank_score(cls, prediction: Prediction) -> tuple[float, dict]:
        score_component = max(0.0, min(float(prediction.score), 100.0))
        ev = max(0.0, float(prediction.expected_value or 0))
        edge = max(0.0, float(prediction.edge or 0))
        ev_component = min(ev / 0.25, 1.0) * 100.0
        edge_component = min(edge / 0.15, 1.0) * 100.0
        data_quality = float((prediction.reasons or {}).get("data_quality_score") or 0.0)
        quote_quality = cls._quote_quality(prediction)
        quality_component = 0.70 * data_quality + 0.30 * quote_quality

        composite = (
            0.40 * score_component
            + 0.25 * ev_component
            + 0.20 * edge_component
            + 0.15 * quality_component
        )
        rationale = {
            "score_component": round(score_component, 2),
            "ev_component": round(ev_component, 2),
            "edge_component": round(edge_component, 2),
            "data_quality": round(data_quality, 2),
            "quote_quality": round(quote_quality, 2),
            "quality_component": round(quality_component, 2),
            "probability": float(prediction.probability),
            "market_odds": float(prediction.market_odds) if prediction.market_odds is not None else None,
            "edge": float(prediction.edge) if prediction.edge is not None else None,
            "expected_value": float(prediction.expected_value) if prediction.expected_value is not None else None,
            "formula": "0.40*score + 0.25*ev + 0.20*edge + 0.15*quality",
        }
        return round(composite, 2), rationale

    @transaction.atomic
    def select(self, target_date: date) -> list[DailyPremiumSelection]:
        start, end = self._bounds(target_date)
        future_start = max(start, timezone.now())
        candidates = list(
            Prediction.objects.select_related("fixture", "fixture__home_team", "fixture__away_team")
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
        for prediction in candidates:
            tier = self._tier_for(prediction)
            if tier is None:
                continue
            rank_score, rationale = self._rank_score(prediction)
            rationale["premium_tier"] = tier
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
            )
            .filter(target_date=target_date, model_version=self.model_version)
            .order_by("rank")
        )
