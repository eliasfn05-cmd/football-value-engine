from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import DailyPremiumSelection, Prediction, PremiumPublicationLedger
from .premium_selection import DYNAMIC_SCORE_FLOORS, DailyPremiumSelector
from .score_v8 import V8_MODEL_VERSION
from .value_policy import PREMIUM_MIN_EV

# API-Football / provider statuses that must never occupy an operational Premium slot.
# Unknown future statuses are intentionally left alone; kickoff time remains the second guard.
NON_OPERATIONAL_FIXTURE_STATUSES = {
    "1h", "ht", "2h", "et", "bt", "p", "live",
    "ft", "aet", "pen",
    "susp", "suspended",
    "int", "interrupted",
    "pst", "postponed",
    "canc", "cancelled", "canceled",
    "abd", "abandoned",
    "awd", "awarded",
    "wo", "walkover",
}


def normalized_fixture_status(value: str | None) -> str:
    return str(value or "").strip().lower().replace("_", " ")


def fixture_is_operational(fixture) -> bool:
    if fixture.kickoff <= timezone.now():
        return False
    return normalized_fixture_status(fixture.status) not in NON_OPERATIONAL_FIXTURE_STATUSES


class PremiumReplacementService:
    """Maintain up to three *currently actionable* Premium picks.

    Sprint 7.9.5 rules:
    - suspended/postponed/cancelled/started/finished fixtures vacate their slot;
    - the best still-eligible candidate is promoted automatically;
    - every promoted pick is immutable in PremiumPublicationLedger;
    - promotion metadata is written in DailyPremiumSelection.rationale;
    - the historical ledger is never deleted when the active Top changes.
    """

    def __init__(self, *, model_version: str = V8_MODEL_VERSION, max_picks: int = 3):
        self.model_version = model_version
        self.max_picks = max(1, min(int(max_picks), 3))
        self.selector = DailyPremiumSelector(model_version=model_version, max_picks=self.max_picks)

    @staticmethod
    def _publication_snapshot(row, calibration) -> dict:
        prediction = row.prediction
        fixture = prediction.fixture
        return {
            "fixture_id": fixture.id,
            "fixture_external_id": fixture.external_id,
            "home_team": fixture.home_team.name,
            "away_team": fixture.away_team.name,
            "kickoff": fixture.kickoff.isoformat(),
            "fixture_status_at_publication": fixture.status,
            "market": prediction.market,
            "selection": prediction.selection,
            "odds": float(prediction.market_odds),
            "raw_probability": calibration.raw_probability,
            "calibrated_probability": calibration.calibrated_probability,
            "implied_probability": calibration.implied_probability,
            "calibrated_edge": calibration.calibrated_edge,
            "reliable_ev": calibration.reliable_ev,
            "reliability": calibration.reliability,
            "score": float(prediction.score),
            "premium_tier": row.premium_tier,
            "premium_rank_score": float(row.premium_rank_score),
            "rationale": row.rationale or {},
        }

    def _rank_operational_candidates(self, target_date: date):
        start, end = self.selector._bounds(target_date)
        future_start = max(start, timezone.now())
        candidates = list(
            Prediction.objects.select_related(
                "fixture",
                "fixture__home_team",
                "fixture__away_team",
                "fixture__competition_ref",
            ).filter(
                model_version=self.model_version,
                fixture__kickoff__gte=future_start,
                fixture__kickoff__lt=end,
                market_odds__gte=Decimal("1.60"),
                market_odds__lte=Decimal("2.40"),
                edge__isnull=False,
                expected_value__gte=PREMIUM_MIN_EV,
            )
        )
        candidates = [p for p in candidates if fixture_is_operational(p.fixture)]

        ranked = []
        selected_floor = DYNAMIC_SCORE_FLOORS[-1]
        for score_floor in DYNAMIC_SCORE_FLOORS:
            ranked = self.selector._rank_candidates(candidates, score_floor)
            selected_floor = score_floor
            if self.selector._unique_fixture_count(ranked) >= self.max_picks:
                break
        return ranked, selected_floor

    @transaction.atomic
    def reconcile(self, target_date: date, *, trigger: str = "scheduled_reconcile") -> list[DailyPremiumSelection]:
        previous = list(
            DailyPremiumSelection.objects.select_related("prediction", "prediction__fixture")
            .filter(target_date=target_date, model_version=self.model_version)
            .order_by("rank")
        )
        previous_ids = {row.prediction_id for row in previous}
        removed = [
            row for row in previous
            if not fixture_is_operational(row.prediction.fixture)
        ]

        ranked, selected_floor = self._rank_operational_candidates(target_date)
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
        removed_labels = [
            f"{row.prediction.fixture.home_team.name} vs {row.prediction.fixture.away_team.name}"
            for row in removed
        ]
        for index, (prediction, tier, rank_score, rationale) in enumerate(chosen, start=1):
            rationale = dict(rationale or {})
            rationale["selector_dynamic_floor_used"] = selected_floor
            is_promotion = prediction.id not in previous_ids and bool(removed)
            if is_promotion:
                rationale.update({
                    "promotion_reason": "PROMOTED_AFTER_PREMIUM_REMOVAL",
                    "promotion_trigger": trigger,
                    "replaced_premium": removed_labels,
                    "promoted_at": timezone.now().isoformat(),
                })
            rows.append(DailyPremiumSelection(
                target_date=target_date,
                prediction=prediction,
                rank=index,
                premium_tier=tier,
                premium_rank_score=Decimal(f"{rank_score:.2f}"),
                model_version=self.model_version,
                rationale=rationale,
            ))

        if rows:
            DailyPremiumSelection.objects.bulk_create(rows)

        persisted = list(
            DailyPremiumSelection.objects.select_related(
                "prediction",
                "prediction__fixture",
                "prediction__fixture__home_team",
                "prediction__fixture__away_team",
                "prediction__fixture__competition_ref",
            ).filter(target_date=target_date, model_version=self.model_version).order_by("rank")
        )

        # Freeze every official publication, including automatically promoted alternates.
        for row in persisted:
            prediction = row.prediction
            calibration = self.selector.calibrator.calibrate(prediction)
            PremiumPublicationLedger.objects.get_or_create(
                prediction=prediction,
                defaults={
                    "target_date": target_date,
                    "published_rank": row.rank,
                    "premium_tier": row.premium_tier,
                    "premium_rank_score": row.premium_rank_score,
                    "model_version": row.model_version,
                    "market": prediction.market,
                    "selection": prediction.selection,
                    "odds": prediction.market_odds,
                    "snapshot": self._publication_snapshot(row, calibration),
                },
            )
        return persisted
