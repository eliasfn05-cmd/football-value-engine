from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import DailyPremiumSelection, Prediction, PremiumPublicationLedger
from .premium_risk_guard import PremiumRiskGuard
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
    """Maintain up to three currently actionable Premium picks.

    Sprint 7.12.2 stability contract:
    - the first official publication is locked for the day;
    - rerunning the pipeline must NOT re-rank an already published active pick out
      of the Top Premium because odds, score, probability or refreshed features
      moved afterwards;
    - a locked pick only vacates its operational slot when its fixture becomes
      unavailable/started/finished (suspended, postponed, cancelled, etc.);
    - every NEW admission still has to pass all current Deep/value/risk guards;
    - a vacant slot is filled by the best currently eligible candidate;
    - PremiumPublicationLedger is the immutable source of truth for the lock.

    This separates two different concerns that were previously mixed together:
    selection quality is evaluated at publication time, while publication
    stability is preserved afterwards. That prevents a pick from appearing at
    08:30 and disappearing at 09:00 merely because the pipeline was refreshed.
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
            # Every NEW Premium admission must pass the latest professional guard.
            ranked = [item for item in ranked if not PremiumRiskGuard.evaluate(item[0]).blocked]
            selected_floor = score_floor
            if self.selector._unique_fixture_count(ranked) >= self.max_picks:
                break
        return ranked, selected_floor

    def _locked_publications(self, target_date: date) -> list[PremiumPublicationLedger]:
        """Return still-operational official publications in immutable order.

        A publication is deliberately NOT re-evaluated against changing model
        probabilities or odds. Those values are frozen in its ledger snapshot.
        Only fixture availability can unlock/vacate the slot automatically.
        """
        publications = (
            PremiumPublicationLedger.objects.select_related(
                "prediction",
                "prediction__fixture",
                "prediction__fixture__home_team",
                "prediction__fixture__away_team",
                "prediction__fixture__competition_ref",
            )
            .filter(target_date=target_date, model_version=self.model_version)
            .order_by("published_rank", "id")
        )
        locked: list[PremiumPublicationLedger] = []
        seen_fixtures: set[int] = set()
        for publication in publications:
            fixture = publication.prediction.fixture
            if fixture.id in seen_fixtures:
                continue
            if not fixture_is_operational(fixture):
                continue
            locked.append(publication)
            seen_fixtures.add(fixture.id)
            if len(locked) >= self.max_picks:
                break
        return locked

    @transaction.atomic
    def reconcile(self, target_date: date, *, trigger: str = "scheduled_reconcile") -> list[DailyPremiumSelection]:
        previous = list(
            DailyPremiumSelection.objects.select_related(
                "prediction",
                "prediction__fixture",
                "prediction__fixture__home_team",
                "prediction__fixture__away_team",
            )
            .filter(target_date=target_date, model_version=self.model_version)
            .order_by("rank")
        )
        previous_ids = {row.prediction_id for row in previous}

        all_publications = list(
            PremiumPublicationLedger.objects.select_related(
                "prediction",
                "prediction__fixture",
                "prediction__fixture__home_team",
                "prediction__fixture__away_team",
            )
            .filter(target_date=target_date, model_version=self.model_version)
            .order_by("published_rank", "id")
        )
        locked_publications = self._locked_publications(target_date)
        locked_prediction_ids = {row.prediction_id for row in locked_publications}
        locked_fixture_ids = {row.prediction.fixture_id for row in locked_publications}

        removed_publications = [
            row for row in all_publications
            if not fixture_is_operational(row.prediction.fixture)
        ]
        removed_labels = [
            f"{row.prediction.fixture.home_team.name} vs {row.prediction.fixture.away_team.name}"
            for row in removed_publications
        ]

        ranked, selected_floor = self._rank_operational_candidates(target_date)

        # 1) Keep every still-operational official publication first.
        # 2) Use current ranking only to fill genuinely vacant slots.
        chosen_new = []
        seen_fixtures = set(locked_fixture_ids)
        slots_left = max(0, self.max_picks - len(locked_publications))
        if slots_left:
            for item in ranked:
                prediction = item[0]
                if prediction.id in locked_prediction_ids or prediction.fixture_id in seen_fixtures:
                    continue
                chosen_new.append(item)
                seen_fixtures.add(prediction.fixture_id)
                if len(chosen_new) >= slots_left:
                    break

        DailyPremiumSelection.objects.filter(
            target_date=target_date,
            model_version=self.model_version,
        ).delete()

        rows: list[DailyPremiumSelection] = []

        # Rehydrate locked picks from the immutable publication ledger. Their
        # publication-time tier/rank/rationale remain stable across refreshes.
        for publication in locked_publications:
            snapshot = publication.snapshot or {}
            rationale = dict(snapshot.get("rationale") or {})
            rationale["publication_lock"] = {
                "locked": True,
                "source": "PremiumPublicationLedger",
                "published_rank": publication.published_rank,
                "policy": "stable_until_fixture_non_operational",
            }
            rows.append(
                DailyPremiumSelection(
                    target_date=target_date,
                    prediction=publication.prediction,
                    rank=len(rows) + 1,
                    premium_tier=publication.premium_tier,
                    premium_rank_score=publication.premium_rank_score,
                    model_version=self.model_version,
                    rationale=rationale,
                )
            )

        for prediction, tier, rank_score, rationale in chosen_new:
            rationale = dict(rationale or {})
            rationale["selector_dynamic_floor_used"] = selected_floor
            risk = PremiumRiskGuard.evaluate(prediction)
            rationale["sprint_7_11_risk_guard"] = {
                "blocked": risk.blocked,
                "code": risk.code,
                "detail": risk.detail,
            }
            rationale["publication_lock"] = {
                "locked": True,
                "source": "PremiumPublicationLedger",
                "policy": "stable_until_fixture_non_operational",
            }
            is_promotion = bool(removed_publications)
            if is_promotion:
                rationale.update({
                    "promotion_reason": "PROMOTED_AFTER_PREMIUM_REMOVAL",
                    "promotion_trigger": trigger,
                    "replaced_premium": removed_labels,
                    "promoted_at": timezone.now().isoformat(),
                })
            elif previous_ids:
                rationale.update({
                    "promotion_reason": "FILLED_AVAILABLE_PREMIUM_SLOT",
                    "promotion_trigger": trigger,
                    "promoted_at": timezone.now().isoformat(),
                })
            rows.append(
                DailyPremiumSelection(
                    target_date=target_date,
                    prediction=prediction,
                    rank=len(rows) + 1,
                    premium_tier=tier,
                    premium_rank_score=Decimal(f"{rank_score:.2f}"),
                    model_version=self.model_version,
                    rationale=rationale,
                )
            )

        if rows:
            DailyPremiumSelection.objects.bulk_create(rows)

        persisted = list(
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

        # Freeze every NEW official publication. Existing publications are left
        # untouched so their original odds/probability/EV snapshot stays exact.
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
